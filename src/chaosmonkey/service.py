"""Everything a person can do to Chaos Monkey, with the rules about who may do it.

The API and the CLI both call this module, so the rules live in one place:
- roles decide what someone may do at all;
- app owners may only change apps belonging to their own teams;
- the four-eyes rule: an approver can never approve a request they made, or a
  configuration they last changed;
- any change to an app's configuration voids its approval, so what runs live is
  always exactly what was approved.

Every change is written to the audit log in the same transaction as the change.
"""

from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from chaosmonkey import audit
from chaosmonkey.clock import Clock
from chaosmonkey.db import (
    ApprovalRow,
    AppRow,
    Database,
    FreezeWindowRow,
    ScheduledKillRow,
    TerminationRow,
)
from chaosmonkey.domain import Actor, AppConfig, Mode, Role
from chaosmonkey.guards import KillSwitch
from chaosmonkey.settings import Settings
from chaosmonkey.state import (
    INCIDENT_KEY,
    KILL_SWITCH_KEY,
    app_config,
    config_fields,
    current_approval,
    effective_mode,
    read_incident,
    read_kill_switch,
    write_control,
)
from chaosmonkey.views import (
    ApprovalView,
    AppView,
    AuditEntryView,
    FreezeWindowView,
    IncidentView,
    KillSwitchView,
    KillView,
    TerminationView,
    VerifyView,
)


class ServiceError(Exception):
    """Base for refusals that are the caller's to fix. The message is safe to show."""


class NotFoundError(ServiceError):
    pass


class ForbiddenError(ServiceError):
    pass


class ConflictError(ServiceError):
    pass


class InvalidError(ServiceError):
    pass


class Permission(StrEnum):
    READ = "read"
    APP_WRITE = "app.write"
    APPROVAL_REQUEST = "approval.request"
    APPROVAL_DECIDE = "approval.decide"
    HALT = "halt"  # switch on the kill switch, declare an incident
    RESUME = "resume"  # switch off the kill switch, close an incident
    FREEZE_MANAGE = "freeze.manage"


GRANTS: dict[Permission, frozenset[Role]] = {
    Permission.READ: frozenset(Role),
    Permission.APP_WRITE: frozenset({Role.APP_OWNER, Role.ADMIN}),
    Permission.APPROVAL_REQUEST: frozenset({Role.APP_OWNER, Role.ADMIN}),
    # Approvers only: admins configure the platform but do not sign off risk.
    Permission.APPROVAL_DECIDE: frozenset({Role.APPROVER}),
    # Stopping chaos should be easy; starting it again is for operators and admins.
    Permission.HALT: frozenset({Role.APP_OWNER, Role.APPROVER, Role.OPERATOR, Role.ADMIN}),
    Permission.RESUME: frozenset({Role.OPERATOR, Role.ADMIN}),
    Permission.FREEZE_MANAGE: frozenset({Role.OPERATOR, Role.ADMIN}),
}


def allowed(actor: Actor, permission: Permission) -> bool:
    return bool(actor.roles & GRANTS[permission])


def _subject_of(label: str) -> str:
    """'api:alice' and 'cli:alice' are the same person for the four-eyes rule."""
    return label.split(":", 1)[-1]


class ChaosMonkey:
    def __init__(self, settings: Settings, db: Database, clock: Clock) -> None:
        self.settings = settings
        self.db = db
        self.clock = clock

    # ----- helpers ----------------------------------------------------------------------

    def _require(self, actor: Actor, permission: Permission) -> None:
        if not allowed(actor, permission):
            raise ForbiddenError(f"Your roles do not allow {permission.value}")

    def _require_team(self, actor: Actor, team: str) -> None:
        if Role.ADMIN in actor.roles:
            return
        if team not in actor.teams:
            raise ForbiddenError(f"Only members of team {team} (or an admin) can do this")

    def _audit(
        self,
        session: Session,
        actor: Actor,
        action: str,
        subject: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        audit.append(
            session,
            at=self.clock.now(),
            actor=actor.label,
            action=action,
            subject=subject,
            details=details or {},
            key=self.settings.audit_key_bytes,
        )

    def _app_row(self, session: Session, name: str) -> AppRow:
        row = session.get(AppRow, name)
        if row is None:
            raise NotFoundError(f"No app called {name}")
        return row

    def _app_view(self, session: Session, row: AppRow) -> AppView:
        config = app_config(row)
        approval = current_approval(session, row, self.clock.now())
        mode, reason = effective_mode(self.settings, config, approval)
        return AppView(
            config=config,
            config_version=row.config_version,
            updated_at=row.updated_at,
            updated_by=row.updated_by,
            effective_mode=mode,
            mode_reason=reason,
            approval=_approval_view(approval) if approval else None,
        )

    # ----- apps -------------------------------------------------------------------------

    def list_apps(self, actor: Actor) -> list[AppView]:
        self._require(actor, Permission.READ)
        with self.db.transaction() as session:
            rows = session.scalars(select(AppRow).order_by(AppRow.name)).all()
            return [self._app_view(session, row) for row in rows]

    def get_app(self, actor: Actor, name: str) -> AppView:
        self._require(actor, Permission.READ)
        with self.db.transaction() as session:
            return self._app_view(session, self._app_row(session, name))

    def save_app(
        self, actor: Actor, config: AppConfig, expected_version: int | None = None
    ) -> AppView:
        """Create or change an app. Any change voids its approval."""
        self._require(actor, Permission.APP_WRITE)
        self._require_team(actor, config.owner_team)
        now = self.clock.now()
        with self.db.transaction() as session:
            row = session.get(AppRow, config.name, with_for_update=True)
            if row is None:
                if expected_version not in (None, 0):
                    raise ConflictError(f"App {config.name} does not exist yet")
                row = AppRow(
                    name=config.name,
                    config_version=1,
                    updated_at=now,
                    updated_by=actor.label,
                    **config_fields(config),
                )
                session.add(row)
                self._audit(session, actor, "app.created", config.name, config_fields(config))
                session.flush()
                return self._app_view(session, row)

            self._require_team(actor, row.owner_team)
            if expected_version is not None and expected_version != row.config_version:
                raise ConflictError(
                    f"App {config.name} is at version {row.config_version}, not "
                    f"{expected_version}. Reload it and try again."
                )
            before = config_fields(app_config(row))
            after = config_fields(config)
            changes = {
                k: {"from": before[k], "to": after[k]} for k in after if before[k] != after[k]
            }
            if not changes:
                return self._app_view(session, row)

            had_approval = current_approval(session, row, now) is not None
            for key, value in after.items():
                setattr(row, key, value)
            row.config_version += 1
            row.updated_at = now
            row.updated_by = actor.label
            superseded = session.scalars(
                select(ApprovalRow).where(
                    ApprovalRow.app_name == row.name, ApprovalRow.status == "pending"
                )
            ).all()
            for pending in superseded:
                pending.status = "superseded"
            self._audit(
                session,
                actor,
                "app.updated",
                config.name,
                {
                    "version": row.config_version,
                    "changes": changes,
                    "approval_voided": had_approval,
                    "pending_requests_superseded": [p.id for p in superseded],
                },
            )
            return self._app_view(session, row)

    # ----- approvals (maker-checker) ----------------------------------------------------

    def request_approval(self, actor: Actor, app_name: str, reason: str) -> ApprovalView:
        self._require(actor, Permission.APPROVAL_REQUEST)
        reason = reason.strip()
        if len(reason) < 10:
            raise InvalidError("Explain why in at least 10 characters")
        now = self.clock.now()
        with self.db.transaction() as session:
            row = self._app_row(session, app_name)
            self._require_team(actor, row.owner_team)
            if not row.enabled or row.mode != Mode.LIVE.value:
                raise InvalidError(
                    "Enable the app and set its mode to live before asking to go live"
                )
            if current_approval(session, row, now) is not None:
                raise ConflictError("This configuration is already approved")
            existing = session.scalars(
                select(ApprovalRow).where(
                    ApprovalRow.app_name == app_name,
                    ApprovalRow.status == "pending",
                    ApprovalRow.config_version == row.config_version,
                )
            ).first()
            if existing is not None:
                raise ConflictError(f"Request {existing.id} is already waiting for a decision")
            approval = ApprovalRow(
                app_name=app_name,
                config_version=row.config_version,
                status="pending",
                reason=reason,
                requested_by=actor.label,
                requested_at=now,
                decided_by=None,
                decided_at=None,
                decision_note=None,
                expires_at=None,
            )
            session.add(approval)
            session.flush()
            self._audit(
                session,
                actor,
                "approval.requested",
                app_name,
                {"approval_id": approval.id, "version": row.config_version, "reason": reason},
            )
            return _approval_view(approval)

    def decide_approval(
        self, actor: Actor, approval_id: int, *, approve: bool, note: str
    ) -> ApprovalView:
        self._require(actor, Permission.APPROVAL_DECIDE)
        note = note.strip()
        if len(note) < 3:
            raise InvalidError("Add a note of at least 3 characters explaining the decision")
        now = self.clock.now()
        with self.db.transaction() as session:
            approval = session.get(ApprovalRow, approval_id, with_for_update=True)
            if approval is None:
                raise NotFoundError(f"No approval request {approval_id}")
            if approval.status != "pending":
                raise ConflictError(f"Request {approval_id} is already {approval.status}")
            row = self._app_row(session, approval.app_name)
            if actor.subject in {
                _subject_of(approval.requested_by),
                _subject_of(row.updated_by),
            }:
                raise ForbiddenError(
                    "Four-eyes rule: you cannot decide on a request you made or a "
                    "configuration you last changed"
                )
            if approval.config_version != row.config_version:
                raise ConflictError("The app has changed since this request; ask for a new one")
            approval.status = "approved" if approve else "rejected"
            approval.decided_by = actor.label
            approval.decided_at = now
            approval.decision_note = note
            if approve:
                approval.expires_at = now + timedelta(days=self.settings.approval_valid_days)
            self._audit(
                session,
                actor,
                f"approval.{approval.status}",
                row.name,
                {
                    "approval_id": approval_id,
                    "version": approval.config_version,
                    "note": note,
                    "expires_at": approval.expires_at.isoformat() if approval.expires_at else None,
                },
            )
            return _approval_view(approval)

    def list_approvals(self, actor: Actor, status: str | None = None) -> list[ApprovalView]:
        self._require(actor, Permission.READ)
        with self.db.transaction() as session:
            query = select(ApprovalRow).order_by(ApprovalRow.id.desc()).limit(200)
            if status:
                query = query.where(ApprovalRow.status == status)
            return [_approval_view(row) for row in session.scalars(query)]

    # ----- kill switch and incidents ------------------------------------------------------

    def kill_switch(self, actor: Actor) -> KillSwitchView:
        self._require(actor, Permission.READ)
        with self.db.transaction() as session:
            return KillSwitchView(**read_kill_switch(session).__dict__)

    def engage_kill_switch(self, actor: Actor, reason: str) -> KillSwitchView:
        self._require(actor, Permission.HALT)
        return self._set_kill_switch(actor, engaged=True, reason=reason)

    def release_kill_switch(self, actor: Actor, reason: str) -> KillSwitchView:
        self._require(actor, Permission.RESUME)
        return self._set_kill_switch(actor, engaged=False, reason=reason)

    def _set_kill_switch(self, actor: Actor, *, engaged: bool, reason: str) -> KillSwitchView:
        reason = _required_text(reason, "reason")
        now = self.clock.now()
        state = KillSwitch(engaged=engaged, reason=reason, by=actor.label, at=now.isoformat())
        with self.db.transaction() as session:
            write_control(session, KILL_SWITCH_KEY, state.__dict__, now)
            action = "kill_switch.engaged" if engaged else "kill_switch.released"
            self._audit(session, actor, action, "global", {"reason": reason})
        return KillSwitchView(**state.__dict__)

    def incident(self, actor: Actor) -> IncidentView:
        self._require(actor, Permission.READ)
        with self.db.transaction() as session:
            value = read_incident(session)
            return IncidentView(**value) if value else IncidentView(open=False)

    def declare_incident(self, actor: Actor, summary: str) -> IncidentView:
        self._require(actor, Permission.HALT)
        return self._set_incident(actor, is_open=True, text=summary)

    def close_incident(self, actor: Actor, note: str) -> IncidentView:
        self._require(actor, Permission.RESUME)
        return self._set_incident(actor, is_open=False, text=note)

    def _set_incident(self, actor: Actor, *, is_open: bool, text: str) -> IncidentView:
        text = _required_text(text, "summary" if is_open else "note")
        now = self.clock.now()
        value = {"open": is_open, "summary": text, "by": actor.label, "at": now.isoformat()}
        with self.db.transaction() as session:
            write_control(session, INCIDENT_KEY, value, now)
            action = "incident.declared" if is_open else "incident.closed"
            self._audit(session, actor, action, "global", {"text": text})
        return IncidentView(**value)

    # ----- freeze windows ---------------------------------------------------------------

    def freeze_windows(self, actor: Actor) -> list[FreezeWindowView]:
        self._require(actor, Permission.READ)
        now = self.clock.now()
        with self.db.transaction() as session:
            rows = session.scalars(
                select(FreezeWindowRow)
                .where(FreezeWindowRow.ends_at > now)
                .order_by(FreezeWindowRow.starts_at)
            )
            return [_freeze_view(row) for row in rows]

    def add_freeze_window(
        self, actor: Actor, name: str, starts_at: datetime, ends_at: datetime
    ) -> FreezeWindowView:
        self._require(actor, Permission.FREEZE_MANAGE)
        name = _required_text(name, "name")
        if starts_at.tzinfo is None or ends_at.tzinfo is None:
            raise InvalidError("Give freeze window times with a time zone")
        if ends_at <= starts_at:
            raise InvalidError("A freeze window must end after it starts")
        with self.db.transaction() as session:
            row = FreezeWindowRow(
                name=name,
                starts_at=starts_at,
                ends_at=ends_at,
                created_by=actor.label,
                created_at=self.clock.now(),
            )
            session.add(row)
            session.flush()
            self._audit(
                session,
                actor,
                "freeze.added",
                name,
                {"id": row.id, "starts_at": starts_at.isoformat(), "ends_at": ends_at.isoformat()},
            )
            return _freeze_view(row)

    def remove_freeze_window(self, actor: Actor, window_id: int) -> None:
        self._require(actor, Permission.FREEZE_MANAGE)
        with self.db.transaction() as session:
            row = session.get(FreezeWindowRow, window_id)
            if row is None:
                raise NotFoundError(f"No freeze window {window_id}")
            self._audit(session, actor, "freeze.removed", row.name, {"id": window_id})
            session.delete(row)

    # ----- history ----------------------------------------------------------------------

    def schedule(self, actor: Actor, day: date | None = None) -> list[KillView]:
        self._require(actor, Permission.READ)
        with self.db.transaction() as session:
            query = select(ScheduledKillRow).order_by(ScheduledKillRow.run_at.desc()).limit(500)
            if day is not None:
                query = query.where(ScheduledKillRow.day == day)
            return [
                KillView(
                    id=r.id,
                    day=r.day,
                    app_name=r.app_name,
                    group=r.group_key,
                    run_at=r.run_at,
                    status=r.status,
                    outcome=r.outcome,
                )
                for r in session.scalars(query)
            ]

    def terminations(self, actor: Actor, limit: int = 100) -> list[TerminationView]:
        self._require(actor, Permission.READ)
        with self.db.transaction() as session:
            rows = session.scalars(
                select(TerminationRow)
                .order_by(TerminationRow.executed_at.desc())
                .limit(min(limit, 500))
            )
            return [
                TerminationView(
                    id=r.id,
                    app_name=r.app_name,
                    group=r.group_key,
                    target_id=r.target_id,
                    mode=r.mode,
                    executed_at=r.executed_at,
                    recovery_status=r.recovery_status,
                    recovered_at=r.recovered_at,
                )
                for r in rows
            ]

    def audit_log(self, actor: Actor, after_seq: int = 0, limit: int = 100) -> list[AuditEntryView]:
        self._require(actor, Permission.READ)
        with self.db.transaction() as session:
            found = audit.entries(session, after_seq=after_seq, limit=min(limit, 500))
            return [AuditEntryView(**entry.__dict__) for entry in found]

    def verify_audit(self, actor: Actor, expect_head: tuple[int, str] | None = None) -> VerifyView:
        self._require(actor, Permission.READ)
        with self.db.transaction() as session:
            report = audit.verify(
                session, key=self.settings.audit_key_bytes, expect_head=expect_head
            )
            return VerifyView(**report.__dict__)


def _required_text(text: str, field: str, limit: int = 500) -> str:
    text = text.strip()
    if len(text) < 3:
        raise InvalidError(f"Give a {field} of at least 3 characters")
    if len(text) > limit:
        raise InvalidError(f"Keep the {field} under {limit} characters")
    return text


def _approval_view(row: ApprovalRow) -> ApprovalView:
    return ApprovalView(
        id=row.id,
        app_name=row.app_name,
        config_version=row.config_version,
        status=row.status,
        reason=row.reason,
        requested_by=row.requested_by,
        requested_at=row.requested_at,
        decided_by=row.decided_by,
        decided_at=row.decided_at,
        decision_note=row.decision_note,
        expires_at=row.expires_at,
    )


def _freeze_view(row: FreezeWindowRow) -> FreezeWindowView:
    return FreezeWindowView(
        id=row.id,
        name=row.name,
        starts_at=row.starts_at,
        ends_at=row.ends_at,
        created_by=row.created_by,
    )
