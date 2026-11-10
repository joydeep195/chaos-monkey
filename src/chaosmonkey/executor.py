"""The executor: plans each day, carries out due kills, and watches for recovery.

One tick does three things, in order:
1. Plan today, once, using Netflix's algorithm (scheduler.py).
2. Check earlier live kills. If a group has not recovered by its deadline, switch on
   the kill switch so nothing else happens until a person has looked.
3. Carry out kills that are due, each only if every safety guard agrees.

For a live kill the kill switch is read again immediately before the platform call,
and an intent entry is written to the audit log first, so even a crash mid-kill
leaves a record.
"""

import logging
import os
import random
import socket
import threading
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from chaosmonkey import audit
from chaosmonkey.clock import Clock
from chaosmonkey.db import AppRow, Database, ScheduleDayRow, ScheduledKillRow, TerminationRow
from chaosmonkey.domain import SYSTEM_ACTOR, GroupKey, Mode, Target, eligible_targets
from chaosmonkey.drivers.base import Driver
from chaosmonkey.guards import KillSwitch, Situation, evaluate
from chaosmonkey.scheduler import plan_day
from chaosmonkey.settings import Settings
from chaosmonkey.state import (
    KILL_SWITCH_KEY,
    acquire_lease,
    active_freeze_window,
    app_config,
    current_approval,
    effective_mode,
    read_incident,
    read_kill_switch,
    release_lease,
    write_control,
)
from chaosmonkey.working_time import WorkingTime

log = logging.getLogger(__name__)

LEASE_NAME = "executor"


class IncidentSource(Protocol):
    """An external system that knows whether an incident is open (PagerDuty and others, M3)."""

    name: str

    def open_incident(self) -> str | None: ...


@dataclass
class TickReport:
    lines: list[str] = field(default_factory=list)

    def add(self, line: str) -> None:
        log.info(line)
        self.lines.append(line)


class Executor:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        driver: Driver,
        clock: Clock,
        *,
        rng: random.Random | None = None,
        incident_sources: Sequence[IncidentSource] = (),
        holder: str | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.driver = driver
        self.clock = clock
        # SystemRandom: the schedule cannot be predicted from earlier choices.
        self.rng = rng or random.SystemRandom()
        self.incident_sources = list(incident_sources)
        self.holder = holder or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.working_time = WorkingTime.from_settings(settings)
        self.audit_key = settings.audit_key_bytes

    # ----- leadership -------------------------------------------------------------------

    def take_lease(self) -> bool:
        ttl = timedelta(seconds=self.settings.tick_seconds * 3)
        with self.db.transaction() as session:
            return acquire_lease(session, LEASE_NAME, self.holder, self.clock.now(), ttl)

    def release(self) -> None:
        with self.db.transaction() as session:
            release_lease(session, LEASE_NAME, self.holder)

    def run_forever(self, stop: threading.Event) -> None:
        log.info("Executor started as %s", self.holder)
        while not stop.is_set():
            try:
                if self.take_lease():
                    self.tick()
            except Exception:
                log.exception("Executor tick failed; will try again next tick")
            stop.wait(self.settings.tick_seconds)
        self.release()
        log.info("Executor stopped")

    # ----- one tick ---------------------------------------------------------------------

    def tick(self) -> TickReport:
        report = TickReport()
        now = self.clock.now()
        targets = self.driver.list_targets()
        self._plan_today(now, targets, report)
        self._watch_recoveries(now, targets, report)
        self._fail_interrupted(now, report)
        with self.db.transaction() as session:
            due = session.scalars(
                select(ScheduledKillRow.id)
                .where(ScheduledKillRow.status == "pending", ScheduledKillRow.run_at <= now)
                .order_by(ScheduledKillRow.run_at)
            ).all()
        for kill_id in due:
            self._execute(kill_id, report)
        return report

    def _audit(self, session: Session, action: str, subject: str, details: dict[str, Any]) -> None:
        audit.append(
            session,
            at=self.clock.now(),
            actor=SYSTEM_ACTOR.label,
            action=action,
            subject=subject,
            details=details,
            key=self.audit_key,
        )

    def _plan_today(self, now: datetime, targets: list[Target], report: TickReport) -> None:
        if not self.settings.schedule_enabled:
            return
        day = self.working_time.local_date(now)
        if not self.working_time.is_working_day(day):
            return
        try:
            with self.db.transaction() as session:
                if session.get(ScheduleDayRow, day) is not None:
                    return
                apps = [app_config(row) for row in session.scalars(select(AppRow))]
                planned = plan_day(
                    day,
                    apps=apps,
                    targets=targets,
                    allowed_accounts=self.settings.allowed_accounts,
                    working_time=self.working_time,
                    rng=self.rng,
                    not_before=now,
                )
                session.add(ScheduleDayRow(day=day, created_at=now, planned=len(planned)))
                for kill in planned:
                    session.add(
                        ScheduledKillRow(
                            day=day,
                            app_name=kill.app,
                            group_key=str(kill.group),
                            run_at=kill.run_at,
                            status="pending",
                            outcome=None,
                            created_at=now,
                        )
                    )
                self._audit(
                    session,
                    "schedule.planned",
                    day.isoformat(),
                    {
                        "count": len(planned),
                        "kills": [
                            {"app": k.app, "group": str(k.group), "run_at": k.run_at.isoformat()}
                            for k in planned
                        ],
                    },
                )
        except IntegrityError:
            return  # Another replica planned the day first.
        report.add(f"Planned {len(planned)} kill(s) for {day.isoformat()}")

    def _watch_recoveries(self, now: datetime, targets: list[Target], report: TickReport) -> None:
        with self.db.transaction() as session:
            watching = session.scalars(
                select(TerminationRow).where(TerminationRow.recovery_status == "watching")
            ).all()
            for kill in watching:
                group = GroupKey.parse(kill.group_key)
                row = session.get(AppRow, kill.app_name)
                pool = eligible_targets(app_config(row), targets) if row else targets
                healthy = sum(1 for t in pool if group.matches(t) and t.healthy)
                if healthy >= kill.healthy_before:
                    kill.recovery_status = "recovered"
                    kill.recovered_at = now
                    seconds = int((now - kill.executed_at).total_seconds())
                    self._audit(
                        session,
                        "kill.recovered",
                        kill.target_id,
                        {"group": kill.group_key, "seconds": seconds, "healthy": healthy},
                    )
                    report.add(f"Recovered: {kill.group_key} is back to {healthy} healthy")
                elif kill.recovery_deadline is not None and now >= kill.recovery_deadline:
                    kill.recovery_status = "failed"
                    reason = (
                        f"{kill.group_key} did not recover within "
                        f"{self.settings.recovery_timeout_minutes} minutes of terminating "
                        f"{kill.target_id}"
                    )
                    self._audit(
                        session,
                        "kill.recovery_failed",
                        kill.target_id,
                        {
                            "group": kill.group_key,
                            "healthy": healthy,
                            "needed": kill.healthy_before,
                        },
                    )
                    self._engage_kill_switch(session, now, f"Automatic halt: {reason}")
                    report.add(f"RECOVERY FAILED: {reason}. Kill switch is now on.")

    def _engage_kill_switch(self, session: Session, now: datetime, reason: str) -> None:
        state = KillSwitch(engaged=True, reason=reason, by=SYSTEM_ACTOR.label, at=now.isoformat())
        write_control(session, KILL_SWITCH_KEY, state.__dict__, now)
        self._audit(session, "kill_switch.engaged", "global", {"reason": reason})

    def _fail_interrupted(self, now: datetime, report: TickReport) -> None:
        """Kills left half-done by a crash are closed as failed, never retried blindly."""
        cutoff = now - timedelta(minutes=self.settings.missed_kill_grace_minutes * 2)
        with self.db.transaction() as session:
            stuck = session.scalars(
                select(ScheduledKillRow).where(
                    ScheduledKillRow.status == "running", ScheduledKillRow.run_at < cutoff
                )
            ).all()
            for kill in stuck:
                kill.status = "failed"
                kill.outcome = "Interrupted: the executor stopped part-way through this kill"
                self._audit(session, "kill.interrupted", kill.group_key, {"kill_id": kill.id})
                report.add(f"Interrupted kill closed as failed: {kill.group_key}")

    def _open_incident(self, session: Session) -> str | None:
        declared = read_incident(session)
        if declared:
            return f"{declared.get('summary', 'declared incident')} (declared by {declared['by']})"
        for source in self.incident_sources:
            try:
                found = source.open_incident()
            except Exception as error:
                # Fail closed: if we cannot tell, assume there is an incident.
                return f"Incident check {source.name} failed ({type(error).__name__})"
            if found:
                return f"{found} (from {source.name})"
        return None

    def _finish(
        self,
        session: Session,
        kill: ScheduledKillRow,
        status: str,
        outcome: str,
        action: str,
        details: dict[str, Any],
    ) -> None:
        kill.status = status
        kill.outcome = outcome
        self._audit(session, action, kill.group_key, {"kill_id": kill.id, **details})

    def _execute(self, kill_id: int, report: TickReport) -> None:
        targets = self.driver.list_targets()
        now = self.clock.now()

        # Step 1: claim the kill and ask every guard.
        with self.db.transaction() as session:
            claimed = session.execute(
                update(ScheduledKillRow)
                .where(ScheduledKillRow.id == kill_id, ScheduledKillRow.status == "pending")
                .values(status="running")
            )
            if claimed.rowcount != 1:  # type: ignore[attr-defined]
                return  # Another replica took it.
            kill = session.get_one(ScheduledKillRow, kill_id)

            if now - kill.run_at > timedelta(minutes=self.settings.missed_kill_grace_minutes):
                outcome = (
                    f"Missed: planned for {kill.run_at.isoformat(timespec='minutes')} but the "
                    "executor was not running then"
                )
                self._finish(session, kill, "skipped", outcome, "kill.skipped", {"guard": "missed"})
                report.add(f"Skipped {kill.group_key}: {outcome}")
                return

            group = GroupKey.parse(kill.group_key)
            row = session.get(AppRow, kill.app_name)
            config = app_config(row) if row else None
            approval = current_approval(session, row, now) if row else None
            if config is None:
                mode, mode_reason = Mode.DRY_RUN, "App is not registered"
                in_group: list[Target] = []
            else:
                mode, mode_reason = effective_mode(self.settings, config, approval)
                in_group = [t for t in eligible_targets(config, targets) if group.matches(t)]

            situation = Situation(
                now=now,
                settings=self.settings,
                working_time=self.working_time,
                app=config,
                group=group,
                mode=mode,
                targets=in_group,
                kill_switch=read_kill_switch(session),
                open_incident=self._open_incident(session),
                freeze_window=active_freeze_window(session, now),
                last_kill_day=self._last_kill_day(session, kill.group_key),
                live_kills_today=self._live_kills_today(session, now),
                live_kills_recovering=self._live_kills_recovering(session),
            )
            verdict = evaluate(situation)
            if not verdict.allowed:
                self._finish(
                    session,
                    kill,
                    "skipped",
                    verdict.reason,
                    "kill.skipped",
                    {"guard": verdict.guard, "reason": verdict.reason, "mode": mode.value},
                )
                report.add(f"Skipped {kill.group_key}: {verdict.reason}")
                return

            healthy = sorted((t for t in in_group if t.healthy), key=lambda t: t.id)
            victim = self.rng.choice(healthy)
            details = {
                "target": victim.id,
                "app": kill.app_name,
                "mode": mode.value,
                "mode_reason": mode_reason,
                "healthy_before": len(healthy),
            }
            if mode is Mode.DRY_RUN:
                session.add(self._termination(kill, victim, mode, len(healthy), now))
                outcome = f"Dry run: would have terminated {victim.id}. {mode_reason}"
                self._finish(session, kill, "done", outcome, "kill.dry_run", details)
                report.add(f"{kill.group_key}: {outcome}")
                return

        # Step 2: last look at the kill switch, and record the intent, right before acting.
        with self.db.transaction() as session:
            kill = session.get_one(ScheduledKillRow, kill_id)
            switch = read_kill_switch(session)
            if switch.engaged:
                outcome = f"Kill switch is on ({switch.reason})"
                self._finish(
                    session, kill, "skipped", outcome, "kill.skipped", {"guard": "kill_switch"}
                )
                report.add(f"Skipped {kill.group_key}: {outcome}")
                return
            self._audit(session, "kill.terminating", victim.id, details)

        # Step 3: act, then record the result.
        try:
            self.driver.terminate(victim)
        except Exception as error:
            message = f"{type(error).__name__}: {str(error)[:300]}"
            with self.db.transaction() as session:
                kill = session.get_one(ScheduledKillRow, kill_id)
                outcome = f"Termination of {victim.id} failed: {message}"
                self._finish(session, kill, "failed", outcome, "kill.failed", details)
            report.add(f"FAILED {kill.group_key}: {outcome}")
            return

        done_at = self.clock.now()
        with self.db.transaction() as session:
            kill = session.get_one(ScheduledKillRow, kill_id)
            session.add(self._termination(kill, victim, mode, len(healthy), done_at))
            outcome = f"Terminated {victim.id}. {mode_reason}"
            self._finish(session, kill, "done", outcome, "kill.terminated", details)
        report.add(f"{kill.group_key}: {outcome}")

    def _termination(
        self, kill: ScheduledKillRow, victim: Target, mode: Mode, healthy: int, at: datetime
    ) -> TerminationRow:
        live = mode is Mode.LIVE
        return TerminationRow(
            scheduled_kill_id=kill.id,
            app_name=kill.app_name,
            group_key=kill.group_key,
            target_id=victim.id,
            mode=mode.value,
            healthy_before=healthy,
            executed_at=at,
            recovery_status="watching" if live else "not_applicable",
            recovery_deadline=(
                at + timedelta(minutes=self.settings.recovery_timeout_minutes) if live else None
            ),
            recovered_at=None,
        )

    def _last_kill_day(self, session: Session, group_key: str) -> date | None:
        last = session.scalars(
            select(TerminationRow.executed_at)
            .where(TerminationRow.group_key == group_key)
            .order_by(TerminationRow.executed_at.desc())
            .limit(1)
        ).first()
        return None if last is None else self.working_time.local_date(last)

    def _live_kills_today(self, session: Session, now: datetime) -> int:
        start, end = self.working_time.day_bounds(self.working_time.local_date(now))
        return int(
            session.scalar(
                select(func.count())
                .select_from(TerminationRow)
                .where(
                    TerminationRow.mode == Mode.LIVE.value,
                    TerminationRow.executed_at >= start,
                    TerminationRow.executed_at < end,
                )
            )
            or 0
        )

    def _live_kills_recovering(self, session: Session) -> int:
        return int(
            session.scalar(
                select(func.count())
                .select_from(TerminationRow)
                .where(TerminationRow.recovery_status == "watching")
            )
            or 0
        )
