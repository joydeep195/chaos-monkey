"""Who may do what: roles, team ownership, the four-eyes rule and approval lifetimes."""

import pytest

from chaosmonkey.clock import FixedClock
from chaosmonkey.domain import AppConfig, Mode
from chaosmonkey.runtime import Runtime
from chaosmonkey.service import ConflictError, ForbiddenError, InvalidError, NotFoundError
from tests.conftest import (
    ADMIN,
    APPROVER,
    OPERATOR,
    OTHER_OWNER,
    OWNER,
    SECOND_APPROVER,
    VIEWER,
    go_live,
    register,
)


def live_request(runtime: Runtime, name: str = "payments-api") -> int:
    register(runtime.service, name, mode=Mode.LIVE)
    return runtime.service.request_approval(OWNER, name, "Quarterly resilience test").id


def test_viewers_cannot_change_anything(runtime: Runtime) -> None:
    with pytest.raises(ForbiddenError):
        runtime.service.save_app(VIEWER, AppConfig(name="x", owner_team="payments"))
    with pytest.raises(ForbiddenError):
        runtime.service.engage_kill_switch(VIEWER, "no")


def test_app_owners_only_touch_their_own_teams(runtime: Runtime) -> None:
    register(runtime.service, "payments-api")
    with pytest.raises(ForbiddenError, match="team payments"):
        runtime.service.save_app(
            OTHER_OWNER, AppConfig(name="payments-api", owner_team="marketing")
        )
    with pytest.raises(ForbiddenError):
        runtime.service.save_app(OTHER_OWNER, AppConfig(name="new-app", owner_team="payments"))
    with pytest.raises(ForbiddenError):
        runtime.service.request_approval(OTHER_OWNER, "payments-api", "Not my app at all")


def test_four_eyes_requester_cannot_approve(runtime: Runtime) -> None:
    from chaosmonkey.domain import Role
    from tests.conftest import make_actor

    both = make_actor("olivia", Role.APPROVER)  # same person as OWNER, with approver rights
    request_id = live_request(runtime)
    with pytest.raises(ForbiddenError, match="Four-eyes"):
        runtime.service.decide_approval(both, request_id, approve=True, note="Looks fine")


def test_four_eyes_last_editor_cannot_approve(runtime: Runtime) -> None:
    from chaosmonkey.domain import Role
    from tests.conftest import make_actor

    editor = make_actor("ada", Role.ADMIN, Role.APPROVER)
    register(runtime.service, "payments-api", mode=Mode.LIVE)
    runtime.service.save_app(
        editor,
        AppConfig(
            name="payments-api",
            owner_team="payments",
            enabled=True,
            mode=Mode.LIVE,
            mean_days_between_kills=3,
        ),
    )
    request_id = runtime.service.request_approval(OWNER, "payments-api", "Ready to go live now").id
    with pytest.raises(ForbiddenError, match="Four-eyes"):
        runtime.service.decide_approval(editor, request_id, approve=True, note="Fine by me")
    runtime.service.decide_approval(SECOND_APPROVER, request_id, approve=True, note="Checked")


def test_admins_cannot_approve(runtime: Runtime) -> None:
    request_id = live_request(runtime)
    with pytest.raises(ForbiddenError):
        runtime.service.decide_approval(ADMIN, request_id, approve=True, note="Admin override")


def test_approval_requires_enabled_live_app_and_a_reason(runtime: Runtime) -> None:
    register(runtime.service, "payments-api")  # dry run
    with pytest.raises(InvalidError, match="live"):
        runtime.service.request_approval(OWNER, "payments-api", "Going live please")
    register(runtime.service, "payments-api", mode=Mode.LIVE)
    with pytest.raises(InvalidError, match="10 characters"):
        runtime.service.request_approval(OWNER, "payments-api", "short")


def test_duplicate_and_already_approved_requests_conflict(runtime: Runtime) -> None:
    live_request(runtime)
    with pytest.raises(ConflictError, match="already waiting"):
        runtime.service.request_approval(OWNER, "payments-api", "Asking a second time")
    go_live(runtime.service, "statements", team="risk")
    with pytest.raises(ConflictError, match="already approved"):
        runtime.service.request_approval(OWNER, "statements", "Asking again after approval")


def test_rejection_keeps_dry_run(runtime: Runtime) -> None:
    request_id = live_request(runtime)
    runtime.service.decide_approval(APPROVER, request_id, approve=False, note="Runbook missing")
    assert runtime.service.get_app(VIEWER, "payments-api").effective_mode is Mode.DRY_RUN
    with pytest.raises(ConflictError):
        runtime.service.decide_approval(SECOND_APPROVER, request_id, approve=True, note="Too late")


def test_changing_the_app_supersedes_pending_requests(runtime: Runtime) -> None:
    request_id = live_request(runtime)
    register(runtime.service, "payments-api", mode=Mode.LIVE, mean_days_between_kills=5)
    statuses = {a.id: a.status for a in runtime.service.list_approvals(VIEWER)}
    assert statuses[request_id] == "superseded"
    with pytest.raises(ConflictError):
        runtime.service.decide_approval(APPROVER, request_id, approve=True, note="Approve old")


def test_approval_expires(runtime: Runtime, clock: FixedClock) -> None:
    assert go_live(runtime.service, "payments-api").effective_mode is Mode.LIVE
    clock.advance(days=91)
    view = runtime.service.get_app(VIEWER, "payments-api")
    assert view.effective_mode is Mode.DRY_RUN
    assert "waiting for approval" in view.mode_reason


def test_saving_the_same_config_does_not_void_approval(runtime: Runtime) -> None:
    view = go_live(runtime.service, "payments-api")
    again = runtime.service.save_app(OWNER, view.config)
    assert again.config_version == view.config_version
    assert again.effective_mode is Mode.LIVE


def test_stale_writes_are_refused(runtime: Runtime) -> None:
    view = register(runtime.service, "payments-api")
    changed = view.config.model_copy(update={"mean_days_between_kills": 7.0})
    runtime.service.save_app(OWNER, changed, expected_version=view.config_version)
    with pytest.raises(ConflictError, match="version"):
        runtime.service.save_app(
            OWNER,
            view.config.model_copy(update={"min_healthy": 3}),
            expected_version=view.config_version,
        )


def test_stopping_is_easy_and_resuming_is_restricted(runtime: Runtime) -> None:
    runtime.service.engage_kill_switch(OWNER, "Deploying a risky change")
    with pytest.raises(ForbiddenError):
        runtime.service.release_kill_switch(OWNER, "Done")
    runtime.service.release_kill_switch(OPERATOR, "Change deployed safely")
    assert not runtime.service.kill_switch(VIEWER).engaged
    runtime.service.declare_incident(APPROVER, "Card network outage")
    with pytest.raises(ForbiddenError):
        runtime.service.close_incident(APPROVER, "Over")
    runtime.service.close_incident(OPERATOR, "Network restored")


def test_freeze_window_validation(runtime: Runtime, clock: FixedClock) -> None:
    from datetime import datetime, timedelta

    start = clock.now()
    with pytest.raises(InvalidError, match="end after"):
        runtime.service.add_freeze_window(ADMIN, "Backwards", start, start - timedelta(hours=1))
    with pytest.raises(InvalidError, match="time zone"):
        naive_start = datetime(2026, 11, 30)  # noqa: DTZ001 - deliberately missing a zone
        naive_end = datetime(2026, 12, 1)  # noqa: DTZ001
        runtime.service.add_freeze_window(ADMIN, "Naive", naive_start, naive_end)
    with pytest.raises(ForbiddenError):
        runtime.service.add_freeze_window(OWNER, "Mine", start, start + timedelta(days=1))
    window = runtime.service.add_freeze_window(ADMIN, "Diwali", start, start + timedelta(days=2))
    assert [w.name for w in runtime.service.freeze_windows(VIEWER)] == ["Diwali"]
    runtime.service.remove_freeze_window(OPERATOR, window.id)
    with pytest.raises(NotFoundError):
        runtime.service.remove_freeze_window(OPERATOR, window.id)


def test_every_change_is_audited(runtime: Runtime) -> None:
    go_live(runtime.service, "payments-api")
    runtime.service.engage_kill_switch(OPERATOR, "Drill")
    entries = runtime.service.audit_log(VIEWER)
    assert [e.action for e in entries] == [
        "app.created",
        "approval.requested",
        "approval.approved",
        "kill_switch.engaged",
    ]
    assert entries[2].actor == "api:arjun"
    assert runtime.service.verify_audit(VIEWER).ok
