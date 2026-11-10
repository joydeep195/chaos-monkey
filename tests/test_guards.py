"""Each safety guard refuses for its own reason, and all of them must agree."""

from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any

import pytest

from chaosmonkey.domain import AppConfig, GroupKey, Mode, Target
from chaosmonkey.guards import GUARDS, KillSwitch, Situation, evaluate
from chaosmonkey.settings import Settings
from chaosmonkey.working_time import WorkingTime

TUESDAY_NOON = datetime(2026, 11, 10, 12, tzinfo=UTC)


def situation(**changes: Any) -> Situation:
    settings = Settings(
        enabled=True, allowed_accounts=("prod",), max_kills_per_day=3, max_concurrent_kills=1
    )
    base = Situation(
        now=TUESDAY_NOON,
        settings=settings,
        working_time=WorkingTime.from_settings(settings),
        app=AppConfig(name="pay", owner_team="payments", enabled=True, min_healthy=2),
        group=GroupKey("pay", "prod", "r1", "", "pay"),
        mode=Mode.LIVE,
        targets=[Target(f"i{n}", "pay", "prod", "r1") for n in range(4)],
        kill_switch=KillSwitch(engaged=False),
        open_incident=None,
        freeze_window=None,
        last_kill_day=None,
        live_kills_today=0,
        live_kills_recovering=0,
    )
    return replace(base, **changes)


def test_everything_in_order_passes() -> None:
    assert evaluate(situation()).allowed


@pytest.mark.parametrize(
    ("changes", "guard"),
    [
        ({"kill_switch": KillSwitch(True, "drill", "omar")}, "kill_switch"),
        ({"settings": Settings(enabled=False, allowed_accounts=("prod",))}, "master_switch"),
        ({"app": None}, "app_opted_in"),
        ({"app": AppConfig(name="pay", owner_team="payments", enabled=False)}, "app_opted_in"),
        ({"group": GroupKey("pay", "dev", "r1", "", "pay")}, "account_allowed"),
        ({"now": datetime(2026, 11, 10, 20, tzinfo=UTC)}, "working_hours"),
        ({"now": datetime(2026, 11, 14, 12, tzinfo=UTC)}, "working_hours"),
        ({"freeze_window": "Month-end close"}, "freeze_window"),
        ({"open_incident": "Card payments degraded"}, "no_open_incident"),
        ({"last_kill_day": date(2026, 11, 10)}, "minimum_gap"),
        ({"live_kills_today": 3}, "daily_cap"),
        ({"live_kills_recovering": 1}, "concurrency_cap"),
        ({"targets": []}, "group_healthy"),
        (
            {
                "targets": [Target("i0", "pay", "prod", "r1", healthy=False)] * 1
                + [Target("i1", "pay", "prod", "r1")] * 3
            },
            "group_healthy",
        ),
        ({"targets": [Target(f"i{n}", "pay", "prod", "r1") for n in range(2)]}, "capacity"),
    ],
)
def test_each_guard_refuses_with_a_reason(changes: dict[str, Any], guard: str) -> None:
    verdict = evaluate(situation(**changes))
    assert not verdict.allowed
    assert verdict.guard == guard
    assert verdict.reason


def test_minimum_gap_counts_working_days() -> None:
    # Killed Monday; Tuesday is one working day later, which satisfies a one-day minimum.
    assert evaluate(situation(last_kill_day=date(2026, 11, 9))).allowed


def test_caps_do_not_apply_to_dry_runs() -> None:
    assert evaluate(situation(mode=Mode.DRY_RUN, live_kills_today=99)).allowed
    assert evaluate(situation(mode=Mode.DRY_RUN, live_kills_recovering=99)).allowed


def test_guard_list_is_complete() -> None:
    assert len(GUARDS) == 12
    assert GUARDS[0][0] == "kill_switch"
