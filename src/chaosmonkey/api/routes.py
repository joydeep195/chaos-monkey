"""HTTP endpoints. Each one signs the caller in and hands over to the service layer,
which checks roles, team ownership and the four-eyes rule.
"""

import logging
from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from chaosmonkey.api.auth import AuthError, TokenVerifier
from chaosmonkey.domain import Actor, AppConfig
from chaosmonkey.service import ChaosMonkey, InvalidError
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

log = logging.getLogger(__name__)
bearer = HTTPBearer(auto_error=False, description="Token from your identity provider")
MAX_TOKEN_LENGTH = 16_384

AppNamePath = Annotated[str, Path(pattern=r"^[a-z][a-z0-9-]{0,62}$")]
ShortText = Annotated[str, Field(min_length=3, max_length=1_000)]


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AppWrite(Body):
    config: AppConfig
    # Send the version you last read; a mismatch means someone else changed it (409).
    expected_version: int | None = None


class ReasonBody(Body):
    reason: ShortText


class NoteBody(Body):
    note: ShortText


class SummaryBody(Body):
    summary: ShortText


class FreezeWindowBody(Body):
    name: Annotated[str, Field(min_length=3, max_length=200)]
    starts_at: datetime
    ends_at: datetime


class Me(BaseModel):
    subject: str
    display_name: str
    roles: list[str]
    teams: list[str]


def get_service(request: Request) -> ChaosMonkey:
    service: ChaosMonkey = request.app.state.runtime.service
    return service


def get_actor(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Actor:
    challenge = {"WWW-Authenticate": "Bearer"}
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in required", headers=challenge)
    if len(credentials.credentials) > MAX_TOKEN_LENGTH:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token too long", headers=challenge)
    verifier: TokenVerifier = request.app.state.verifier
    try:
        return verifier.verify(credentials.credentials)
    except AuthError as error:
        client = request.client.host if request.client else "unknown"
        log.warning("Rejected token from %s: %s", client, error)
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            str(error),
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        ) from error


ActorDep = Annotated[Actor, Depends(get_actor)]
ServiceDep = Annotated[ChaosMonkey, Depends(get_service)]

health = APIRouter(tags=["health"])
api = APIRouter(prefix="/api/v1")


@health.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness: the process is up."""
    return {"status": "ok"}


@health.get("/readyz")
def readyz(service: ServiceDep) -> dict[str, str]:
    """Readiness: the database answers."""
    try:
        with service.db.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as error:
        log.exception("Readiness check failed")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Database unavailable") from error
    return {"status": "ready"}


@api.get("/me", tags=["people"])
def me(actor: ActorDep) -> Me:
    return Me(
        subject=actor.subject,
        display_name=actor.display_name,
        roles=sorted(actor.roles),
        teams=sorted(actor.teams),
    )


@api.get("/apps", tags=["apps"])
def list_apps(actor: ActorDep, service: ServiceDep) -> list[AppView]:
    return service.list_apps(actor)


@api.get("/apps/{name}", tags=["apps"])
def get_app(name: AppNamePath, actor: ActorDep, service: ServiceDep) -> AppView:
    return service.get_app(actor, name)


@api.put("/apps/{name}", tags=["apps"])
def save_app(name: AppNamePath, body: AppWrite, actor: ActorDep, service: ServiceDep) -> AppView:
    if body.config.name != name:
        raise InvalidError("The app name in the body must match the address")
    return service.save_app(actor, body.config, body.expected_version)


@api.post("/apps/{name}/approval-requests", tags=["approvals"], status_code=status.HTTP_201_CREATED)
def request_approval(
    name: AppNamePath, body: ReasonBody, actor: ActorDep, service: ServiceDep
) -> ApprovalView:
    return service.request_approval(actor, name, body.reason)


@api.get("/approvals", tags=["approvals"])
def list_approvals(
    actor: ActorDep,
    service: ServiceDep,
    status_filter: Annotated[
        str | None,
        Query(alias="status", pattern="^(pending|approved|rejected|superseded)$"),
    ] = None,
) -> list[ApprovalView]:
    return service.list_approvals(actor, status_filter)


@api.post("/approvals/{approval_id}/approve", tags=["approvals"])
def approve(
    approval_id: Annotated[int, Path(ge=1)], body: NoteBody, actor: ActorDep, service: ServiceDep
) -> ApprovalView:
    return service.decide_approval(actor, approval_id, approve=True, note=body.note)


@api.post("/approvals/{approval_id}/reject", tags=["approvals"])
def reject(
    approval_id: Annotated[int, Path(ge=1)], body: NoteBody, actor: ActorDep, service: ServiceDep
) -> ApprovalView:
    return service.decide_approval(actor, approval_id, approve=False, note=body.note)


@api.get("/kill-switch", tags=["controls"])
def kill_switch(actor: ActorDep, service: ServiceDep) -> KillSwitchView:
    return service.kill_switch(actor)


@api.post("/kill-switch/engage", tags=["controls"])
def engage(body: ReasonBody, actor: ActorDep, service: ServiceDep) -> KillSwitchView:
    return service.engage_kill_switch(actor, body.reason)


@api.post("/kill-switch/release", tags=["controls"])
def release(body: ReasonBody, actor: ActorDep, service: ServiceDep) -> KillSwitchView:
    return service.release_kill_switch(actor, body.reason)


@api.get("/incident", tags=["controls"])
def incident(actor: ActorDep, service: ServiceDep) -> IncidentView:
    return service.incident(actor)


@api.post("/incident/declare", tags=["controls"])
def declare_incident(body: SummaryBody, actor: ActorDep, service: ServiceDep) -> IncidentView:
    return service.declare_incident(actor, body.summary)


@api.post("/incident/close", tags=["controls"])
def close_incident(body: NoteBody, actor: ActorDep, service: ServiceDep) -> IncidentView:
    return service.close_incident(actor, body.note)


@api.get("/freeze-windows", tags=["controls"])
def freeze_windows(actor: ActorDep, service: ServiceDep) -> list[FreezeWindowView]:
    return service.freeze_windows(actor)


@api.post("/freeze-windows", tags=["controls"], status_code=status.HTTP_201_CREATED)
def add_freeze_window(
    body: FreezeWindowBody, actor: ActorDep, service: ServiceDep
) -> FreezeWindowView:
    return service.add_freeze_window(actor, body.name, body.starts_at, body.ends_at)


@api.delete(
    "/freeze-windows/{window_id}", tags=["controls"], status_code=status.HTTP_204_NO_CONTENT
)
def remove_freeze_window(
    window_id: Annotated[int, Path(ge=1)], actor: ActorDep, service: ServiceDep
) -> None:
    service.remove_freeze_window(actor, window_id)


@api.get("/schedule", tags=["history"])
def schedule(actor: ActorDep, service: ServiceDep, day: date | None = None) -> list[KillView]:
    return service.schedule(actor, day)


@api.get("/terminations", tags=["history"])
def terminations(
    actor: ActorDep, service: ServiceDep, limit: Annotated[int, Query(ge=1, le=500)] = 100
) -> list[TerminationView]:
    return service.terminations(actor, limit)


@api.get("/audit", tags=["audit"])
def audit_log(
    actor: ActorDep,
    service: ServiceDep,
    after_seq: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[AuditEntryView]:
    return service.audit_log(actor, after_seq, limit)


@api.get("/audit/verify", tags=["audit"])
def verify_audit(
    actor: ActorDep,
    service: ServiceDep,
    expect_head: Annotated[str | None, Query(pattern=r"^\d+:[0-9a-f]{64}$")] = None,
) -> VerifyView:
    head = None
    if expect_head:
        seq, digest = expect_head.split(":")
        head = (int(seq), digest)
    return service.verify_audit(actor, head)
