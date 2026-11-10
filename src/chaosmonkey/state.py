"""Reading and writing shared state: app settings, approvals, kill switch, incidents."""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from chaosmonkey.db import ApprovalRow, AppRow, ControlRow, FreezeWindowRow, LeaseRow
from chaosmonkey.domain import AppConfig, ExceptionRule, Grouping, Mode
from chaosmonkey.guards import KillSwitch
from chaosmonkey.settings import Settings

KILL_SWITCH_KEY = "kill_switch"
INCIDENT_KEY = "incident"


def app_config(row: AppRow) -> AppConfig:
    return AppConfig(
        name=row.name,
        owner_team=row.owner_team,
        enabled=row.enabled,
        mode=Mode(row.mode),
        mean_days_between_kills=row.mean_days_between_kills,
        min_days_between_kills=row.min_days_between_kills,
        grouping=Grouping(row.grouping),
        regions_independent=row.regions_independent,
        min_healthy=row.min_healthy,
        exceptions=tuple(ExceptionRule.model_validate(item) for item in row.exceptions),
    )


def config_fields(config: AppConfig) -> dict[str, Any]:
    """The columns of AppRow that come from an AppConfig."""
    return {
        "owner_team": config.owner_team,
        "enabled": config.enabled,
        "mode": config.mode.value,
        "mean_days_between_kills": config.mean_days_between_kills,
        "min_days_between_kills": config.min_days_between_kills,
        "grouping": config.grouping.value,
        "regions_independent": config.regions_independent,
        "min_healthy": config.min_healthy,
        "exceptions": [rule.model_dump() for rule in config.exceptions],
    }


def current_approval(session: Session, row: AppRow, now: datetime) -> ApprovalRow | None:
    """The approval covering the app's current configuration, if one is in force."""
    return session.scalars(
        select(ApprovalRow)
        .where(
            ApprovalRow.app_name == row.name,
            ApprovalRow.status == "approved",
            ApprovalRow.config_version == row.config_version,
            ApprovalRow.expires_at > now,
        )
        .order_by(ApprovalRow.decided_at.desc())
        .limit(1)
    ).first()


def effective_mode(
    settings: Settings, config: AppConfig, approval: ApprovalRow | None
) -> tuple[Mode, str]:
    """Live only when everything lines up; otherwise dry run, with the reason."""
    if not config.enabled:
        return Mode.DRY_RUN, "Off: the app is disabled, so nothing is planned or terminated"
    if settings.leashed:
        return Mode.DRY_RUN, "Dry run: the global leash is on (leashed = true)"
    if config.mode is Mode.DRY_RUN:
        return Mode.DRY_RUN, "Dry run: the app is set to dry run"
    if approval is None or approval.expires_at is None:
        return Mode.DRY_RUN, "Dry run: live mode is waiting for approval of this configuration"
    return (
        Mode.LIVE,
        f"Live: approved by {approval.decided_by} until {approval.expires_at.date().isoformat()}",
    )


def read_kill_switch(session: Session) -> KillSwitch:
    row = session.get(ControlRow, KILL_SWITCH_KEY)
    if row is None:
        return KillSwitch(engaged=False)
    return KillSwitch(**row.value)


def write_control(session: Session, key: str, value: dict[str, Any], now: datetime) -> None:
    row = session.get(ControlRow, key, with_for_update=True)
    if row is None:
        session.add(ControlRow(key=key, value=value, updated_at=now))
    else:
        row.value = value
        row.updated_at = now


def read_incident(session: Session) -> dict[str, Any] | None:
    row = session.get(ControlRow, INCIDENT_KEY)
    if row is None or not row.value.get("open"):
        return None
    return row.value


def active_freeze_window(session: Session, now: datetime) -> str | None:
    row = session.scalars(
        select(FreezeWindowRow)
        .where(FreezeWindowRow.starts_at <= now, FreezeWindowRow.ends_at > now)
        .order_by(FreezeWindowRow.starts_at)
        .limit(1)
    ).first()
    if row is None:
        return None
    return f"{row.name} (until {row.ends_at.isoformat(timespec='minutes')})"


def acquire_lease(session: Session, name: str, holder: str, now: datetime, ttl: timedelta) -> bool:
    """Take or renew a named lease. Only one holder at a time, across all replicas."""
    expires = now + ttl
    taken = session.execute(
        update(LeaseRow)
        .where(LeaseRow.name == name, (LeaseRow.expires_at < now) | (LeaseRow.holder == holder))
        .values(holder=holder, expires_at=expires)
    )
    if taken.rowcount == 1:  # type: ignore[attr-defined]
        return True
    if session.get(LeaseRow, name) is not None:
        return False
    try:
        with session.begin_nested():
            session.add(LeaseRow(name=name, holder=holder, expires_at=expires))
    except IntegrityError:
        return False
    return True


def release_lease(session: Session, name: str, holder: str) -> None:
    lease = session.get(LeaseRow, name)
    if lease is not None and lease.holder == holder:
        session.delete(lease)
