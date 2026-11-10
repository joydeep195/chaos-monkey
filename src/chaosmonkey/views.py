"""What the API and CLI show. Read-only snapshots, safe to serialise."""

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from chaosmonkey.domain import AppConfig, Mode


class View(BaseModel):
    model_config = ConfigDict(frozen=True)


class ApprovalView(View):
    id: int
    app_name: str
    config_version: int
    status: str
    reason: str
    requested_by: str
    requested_at: datetime
    decided_by: str | None
    decided_at: datetime | None
    decision_note: str | None
    expires_at: datetime | None


class AppView(View):
    config: AppConfig
    config_version: int
    updated_at: datetime
    updated_by: str
    effective_mode: Mode
    mode_reason: str
    approval: ApprovalView | None


class KillView(View):
    id: int
    day: date
    app_name: str
    group: str
    run_at: datetime
    status: str
    outcome: str | None


class TerminationView(View):
    id: int
    app_name: str
    group: str
    target_id: str
    mode: str
    executed_at: datetime
    recovery_status: str
    recovered_at: datetime | None


class KillSwitchView(View):
    engaged: bool
    reason: str
    by: str
    at: str


class IncidentView(View):
    open: bool
    summary: str = ""
    by: str = ""
    at: str = ""


class FreezeWindowView(View):
    id: int
    name: str
    starts_at: datetime
    ends_at: datetime
    created_by: str


class AuditEntryView(View):
    seq: int
    at: datetime
    actor: str
    action: str
    subject: str
    details: dict[str, Any]
    prev_hash: str
    hash: str


class VerifyView(View):
    ok: bool
    entries: int
    head_seq: int
    head_hash: str
    problem: str | None
    first_bad_seq: int | None
