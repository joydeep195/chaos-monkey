"""End-to-end runs against the simulated fleet: dry runs, live kills, recovery, halts."""

import random
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from chaosmonkey import executor as executor_module
from chaosmonkey.clock import FixedClock
from chaosmonkey.db import ScheduledKillRow, TerminationRow
from chaosmonkey.domain import Mode
from chaosmonkey.drivers.base import DriverError
from chaosmonkey.executor import Executor
from chaosmonkey.guards import KillSwitch
from chaosmonkey.runtime import Runtime
from tests.conftest import ADMIN, OPERATOR, OWNER, VIEWER, go_live, register

LATE_MONDAY = datetime(2026, 11, 9, 16, 50, tzinfo=UTC)


def make_executor(runtime: Runtime, seed: int = 42, **kwargs: object) -> Executor:
    return Executor(
        runtime.settings,
        runtime.db,
        runtime.driver,
        runtime.clock,
        rng=random.Random(seed),
        **kwargs,  # type: ignore[arg-type]
    )


def plan_and_run(runtime: Runtime, clock: FixedClock, **kwargs: object) -> list[str]:
    """Plan Monday early, then run everything due by late Monday."""
    executor = make_executor(runtime, **kwargs)
    lines = executor.tick().lines
    clock.set(LATE_MONDAY)
    return lines + executor.tick().lines


def kills(runtime: Runtime) -> list[ScheduledKillRow]:
    with runtime.db.transaction() as session:
        return list(session.scalars(select(ScheduledKillRow).order_by(ScheduledKillRow.id)))


def terminations(runtime: Runtime) -> list[TerminationRow]:
    with runtime.db.transaction() as session:
        return list(session.scalars(select(TerminationRow).order_by(TerminationRow.id)))


def alive(runtime: Runtime, app: str) -> int:
    return sum(1 for t in runtime.driver.list_targets() if t.app == app)


def actions(runtime: Runtime) -> list[str]:
    return [e.action for e in runtime.service.audit_log(VIEWER, 0, 500)]


def test_nothing_happens_when_scheduling_is_off(
    make_runtime: Callable[..., Runtime], clock: FixedClock
) -> None:
    runtime = make_runtime(schedule_enabled=False)
    register(runtime.service, "payments-api")
    assert plan_and_run(runtime, clock) == []
    assert kills(runtime) == []


def test_leashed_means_dry_run_even_when_approved(
    make_runtime: Callable[..., Runtime], clock: FixedClock
) -> None:
    runtime = make_runtime(leashed=True)
    go_live(runtime.service, "payments-api")
    plan_and_run(runtime, clock)
    (kill,) = kills(runtime)
    assert kill.status == "done"
    assert "Dry run" in (kill.outcome or "")
    assert "leash" in (kill.outcome or "")
    assert alive(runtime, "payments-api") == 4
    assert terminations(runtime)[0].mode == Mode.DRY_RUN.value
    assert "kill.dry_run" in actions(runtime)


def test_live_mode_without_approval_stays_dry_run(runtime: Runtime, clock: FixedClock) -> None:
    register(runtime.service, "payments-api", mode=Mode.LIVE)
    plan_and_run(runtime, clock)
    assert "waiting for approval" in (kills(runtime)[0].outcome or "")
    assert alive(runtime, "payments-api") == 4


def test_approved_live_kill_terminates_and_recovers(runtime: Runtime, clock: FixedClock) -> None:
    go_live(runtime.service, "payments-api")
    plan_and_run(runtime, clock)
    (kill,) = kills(runtime)
    assert kill.status == "done", kill.outcome
    assert kill.outcome and kill.outcome.startswith("Terminated")
    assert alive(runtime, "payments-api") == 3
    (termination,) = terminations(runtime)
    assert termination.recovery_status == "watching"

    clock.advance(minutes=2)  # the simulator replaces instances after 60 seconds
    lines = make_executor(runtime).tick().lines
    assert any("Recovered" in line for line in lines)
    assert terminations(runtime)[0].recovery_status == "recovered"
    assert alive(runtime, "payments-api") == 4
    for expected in ("kill.terminating", "kill.terminated", "kill.recovered"):
        assert expected in actions(runtime)
    assert runtime.service.verify_audit(VIEWER).ok


def test_failed_recovery_switches_on_the_kill_switch(runtime: Runtime, clock: FixedClock) -> None:
    go_live(runtime.service, "statements", team="risk", min_healthy=1)
    plan_and_run(runtime, clock)
    assert alive(runtime, "statements") == 2

    clock.advance(minutes=11)  # past the 10-minute recovery deadline
    lines = make_executor(runtime).tick().lines
    assert any("RECOVERY FAILED" in line for line in lines)
    switch = runtime.service.kill_switch(VIEWER)
    assert switch.engaged
    assert "statements" in switch.reason
    assert "kill_switch.engaged" in actions(runtime)

    # The next day nothing runs until a person releases the switch.
    clock.set(datetime(2026, 11, 10, 8, tzinfo=UTC))
    plan_and_run_tuesday = make_executor(runtime)
    plan_and_run_tuesday.tick()
    clock.set(datetime(2026, 11, 10, 16, 50, tzinfo=UTC))
    plan_and_run_tuesday.tick()
    tuesday = [k for k in kills(runtime) if k.day == date(2026, 11, 10)]
    assert tuesday and all("Kill switch is on" in (k.outcome or "") for k in tuesday)


def test_config_change_voids_the_approval(runtime: Runtime, clock: FixedClock) -> None:
    go_live(runtime.service, "payments-api")
    view = register(runtime.service, "payments-api", mode=Mode.LIVE, min_healthy=1)
    assert view.effective_mode is Mode.DRY_RUN
    assert view.approval is None
    plan_and_run(runtime, clock)
    assert alive(runtime, "payments-api") == 4


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        (lambda s: s.declare_incident(OPERATOR, "Card payments degraded"), "incident is open"),
        (
            lambda s: s.add_freeze_window(
                ADMIN,
                "Month-end close",
                datetime(2026, 11, 9, tzinfo=UTC),
                datetime(2026, 11, 10, tzinfo=UTC),
            ),
            "Freeze window",
        ),
        (lambda s: s.engage_kill_switch(OWNER, "Release in progress"), "Kill switch is on"),
    ],
)
def test_incidents_freezes_and_the_kill_switch_block_kills(
    runtime: Runtime, clock: FixedClock, setup: Callable[..., object], expected: str
) -> None:
    go_live(runtime.service, "payments-api")
    setup(runtime.service)
    plan_and_run(runtime, clock)
    (kill,) = kills(runtime)
    assert kill.status == "skipped"
    assert expected in (kill.outcome or "")
    assert alive(runtime, "payments-api") == 4


def test_capacity_guard_protects_small_groups(runtime: Runtime, clock: FixedClock) -> None:
    go_live(runtime.service, "kyc", team="onboarding", min_healthy=2)
    plan_and_run(runtime, clock)
    (kill,) = kills(runtime)
    assert kill.status == "skipped"
    assert "below the minimum" in (kill.outcome or "")


def test_protected_and_unlisted_targets_are_never_planned(
    runtime: Runtime, clock: FixedClock
) -> None:
    go_live(runtime.service, "ledger", team="ledger", min_healthy=1)
    register(runtime.service, "sandbox", team="platform", min_healthy=1)
    plan_and_run(runtime, clock)
    assert kills(runtime) == []


def test_kill_switch_is_checked_again_right_before_terminating(
    runtime: Runtime, clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    go_live(runtime.service, "payments-api")
    executor = make_executor(runtime)
    executor.tick()
    clock.set(LATE_MONDAY)
    reads = iter([KillSwitch(False), KillSwitch(True, "pulled mid-kill", "api:omar")])
    monkeypatch.setattr(executor_module, "read_kill_switch", lambda _session: next(reads))
    executor.tick()
    (kill,) = kills(runtime)
    assert kill.status == "skipped"
    assert "pulled mid-kill" in (kill.outcome or "")
    assert alive(runtime, "payments-api") == 4


def test_missed_kills_are_skipped_not_run_late(
    make_runtime: Callable[..., Runtime], clock: FixedClock
) -> None:
    runtime = make_runtime(missed_kill_grace_minutes=1)
    go_live(runtime.service, "payments-api")
    plan_and_run(runtime, clock)
    (kill,) = kills(runtime)
    assert kill.status == "skipped"
    assert (kill.outcome or "").startswith("Missed")


def test_daily_cap_limits_live_kills(
    make_runtime: Callable[..., Runtime], clock: FixedClock
) -> None:
    runtime = make_runtime(max_kills_per_day=1)
    go_live(runtime.service, "payments-api", min_healthy=1)
    go_live(runtime.service, "statements", team="risk", min_healthy=1)
    plan_and_run(runtime, clock)
    statuses = sorted(k.status for k in kills(runtime))
    assert statuses == ["done", "skipped"]
    assert any("Daily limit" in (k.outcome or "") for k in kills(runtime))


def test_concurrency_cap_waits_for_recovery(
    make_runtime: Callable[..., Runtime], clock: FixedClock
) -> None:
    runtime = make_runtime(max_concurrent_kills=1)
    go_live(runtime.service, "payments-api", min_healthy=1)
    go_live(runtime.service, "statements", team="risk", min_healthy=1)
    plan_and_run(runtime, clock)
    assert any("still recovering" in (k.outcome or "") for k in kills(runtime))


def test_minimum_gap_between_kills(runtime: Runtime, clock: FixedClock) -> None:
    go_live(runtime.service, "payments-api", min_days_between_kills=2)
    plan_and_run(runtime, clock)
    clock.advance(minutes=5)
    make_executor(runtime).tick()  # recover
    clock.set(datetime(2026, 11, 10, 8, tzinfo=UTC))
    executor = make_executor(runtime)
    executor.tick()
    clock.set(datetime(2026, 11, 10, 16, 50, tzinfo=UTC))
    executor.tick()
    tuesday = [k for k in kills(runtime) if k.day == date(2026, 11, 10)]
    assert len(tuesday) == 1
    assert "minimum gap" in (tuesday[0].outcome or "")


def test_a_day_is_planned_only_once_even_with_two_executors(
    runtime: Runtime, clock: FixedClock
) -> None:
    register(runtime.service, "payments-api")
    first = make_executor(runtime, seed=1)
    second = make_executor(runtime, seed=2)
    first.tick()
    second.tick()
    assert len(kills(runtime)) == 1
    assert actions(runtime).count("schedule.planned") == 1


def test_only_one_executor_holds_the_lease(runtime: Runtime) -> None:
    first = make_executor(runtime, holder="a")
    second = make_executor(runtime, holder="b")
    assert first.take_lease()
    assert not second.take_lease()
    assert first.take_lease()  # renewing works
    first.release()
    assert second.take_lease()


def test_driver_failure_is_recorded(
    runtime: Runtime, clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    go_live(runtime.service, "payments-api")

    def refuse(_target: object) -> None:
        raise DriverError("API server said no")

    monkeypatch.setattr(runtime.driver, "terminate", refuse)
    plan_and_run(runtime, clock)
    (kill,) = kills(runtime)
    assert kill.status == "failed"
    assert "API server said no" in (kill.outcome or "")
    assert "kill.failed" in actions(runtime)


class BrokenIncidentSource:
    name = "pagerduty"

    def open_incident(self) -> str | None:
        raise TimeoutError


def test_an_unreachable_incident_source_blocks_kills(runtime: Runtime, clock: FixedClock) -> None:
    go_live(runtime.service, "payments-api")
    plan_and_run(runtime, clock, incident_sources=[BrokenIncidentSource()])
    (kill,) = kills(runtime)
    assert kill.status == "skipped"
    assert "pagerduty failed" in (kill.outcome or "")


def test_interrupted_kills_are_closed_as_failed(
    make_runtime: Callable[..., Runtime], clock: FixedClock
) -> None:
    runtime = make_runtime(missed_kill_grace_minutes=30)
    register(runtime.service, "payments-api")
    executor = make_executor(runtime)
    executor.tick()
    with runtime.db.transaction() as session:
        for kill in session.scalars(select(ScheduledKillRow)):
            kill.status = "running"
    clock.set(datetime(2026, 11, 10, 8, tzinfo=UTC))
    executor.tick()
    assert kills(runtime)[0].status == "failed"
    assert "kill.interrupted" in actions(runtime)


def test_audit_chain_survives_a_busy_day(runtime: Runtime, clock: FixedClock) -> None:
    go_live(runtime.service, "payments-api", min_healthy=1)
    go_live(runtime.service, "statements", team="risk", min_healthy=1)
    register(runtime.service, "kyc", team="onboarding")
    plan_and_run(runtime, clock)
    clock.advance(minutes=15)
    make_executor(runtime).tick()
    report = runtime.service.verify_audit(VIEWER)
    assert report.ok
    assert report.entries > 10
    assert clock.now() - LATE_MONDAY == timedelta(minutes=15)
