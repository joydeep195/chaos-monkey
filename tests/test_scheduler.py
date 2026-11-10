"""Netflix's scheduling algorithm: frequency, one kill per group per day, grouping rules."""

import itertools
import random
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from chaosmonkey.domain import AppConfig, ExceptionRule, Grouping, GroupKey, Target
from chaosmonkey.scheduler import discover_groups, plan_day
from chaosmonkey.working_time import WorkingTime

UTC_9_TO_17 = WorkingTime(
    zone=ZoneInfo("UTC"),
    start_hour=9,
    end_hour=17,
    working_days=frozenset(range(5)),
    holidays=frozenset({date(2026, 12, 25)}),
)
MONDAY = date(2026, 11, 9)


def fleet() -> list[Target]:
    """Two regions x two stacks x two clusters, two instances each (synthetic)."""
    targets = []
    for region in ("r1", "r2"):
        for stack in ("blue", "green"):
            for detail in ("web", "worker"):
                for n in (1, 2):
                    targets.append(
                        Target(
                            f"{region}-{stack}-{detail}-{n}", "pay", "prod", region, stack, detail
                        )
                    )
    return targets


def app(**fields: object) -> AppConfig:
    values: dict[str, object] = {"name": "pay", "owner_team": "payments", "enabled": True}
    values.update(fields)
    return AppConfig.model_validate(values)


def test_mean_gap_between_kills_matches_the_setting() -> None:
    rng = random.Random(1234)
    config = app(mean_days_between_kills=4, grouping=Grouping.APP, regions_independent=False)
    targets = fleet()
    kill_days = []
    day = MONDAY
    working_day_index = 0
    while working_day_index < 20_000:
        if UTC_9_TO_17.is_working_day(day):
            planned = plan_day(
                day,
                apps=[config],
                targets=targets,
                allowed_accounts=("prod",),
                working_time=UTC_9_TO_17,
                rng=rng,
            )
            assert len(planned) <= 1
            if planned:
                kill_days.append(working_day_index)
            working_day_index += 1
        day += timedelta(days=1)
    gaps = [b - a for a, b in itertools.pairwise(kill_days)]
    mean_gap = sum(gaps) / len(gaps)
    assert 3.8 < mean_gap < 4.2


def test_mean_of_one_day_kills_every_working_day() -> None:
    planned = plan_day(
        MONDAY,
        apps=[app(mean_days_between_kills=1, grouping=Grouping.APP, regions_independent=False)],
        targets=fleet(),
        allowed_accounts=("prod",),
        working_time=UTC_9_TO_17,
        rng=random.Random(1),
    )
    assert len(planned) == 1


def test_kills_fall_inside_working_hours_and_after_not_before() -> None:
    rng = random.Random(7)
    not_before = datetime(2026, 11, 9, 13, 30, tzinfo=UTC)
    for _ in range(200):
        for kill in plan_day(
            MONDAY,
            apps=[app(mean_days_between_kills=1)],
            targets=fleet(),
            allowed_accounts=("prod",),
            working_time=UTC_9_TO_17,
            rng=rng,
            not_before=not_before,
        ):
            assert not_before <= kill.run_at < datetime(2026, 11, 9, 17, tzinfo=UTC)


def test_no_plan_on_weekends_holidays_or_after_hours() -> None:
    kwargs = {
        "apps": [app(mean_days_between_kills=1)],
        "targets": fleet(),
        "allowed_accounts": ("prod",),
        "working_time": UTC_9_TO_17,
        "rng": random.Random(1),
    }
    assert plan_day(date(2026, 11, 14), **kwargs) == []  # Saturday
    assert plan_day(date(2026, 12, 25), **kwargs) == []  # holiday
    late = datetime(2026, 11, 9, 17, 5, tzinfo=UTC)
    assert plan_day(MONDAY, not_before=late, **kwargs) == []


def test_grouping_decides_how_many_groups_there_are() -> None:
    targets = fleet()

    def count(**fields: object) -> int:
        return len(discover_groups([app(**fields)], targets, ("prod",))["pay"])

    assert count(grouping=Grouping.APP) == 2  # one per region
    assert count(grouping=Grouping.STACK) == 4  # 2 stacks x 2 regions
    assert count(grouping=Grouping.CLUSTER) == 8  # 4 clusters x 2 regions
    assert count(grouping=Grouping.APP, regions_independent=False) == 1
    assert count(grouping=Grouping.CLUSTER, regions_independent=False) == 4


def test_linked_regions_put_both_regions_in_one_group() -> None:
    groups = discover_groups(
        [app(grouping=Grouping.STACK, regions_independent=False)], fleet(), ("prod",)
    )["pay"]
    blue = next(g for g in groups if g.stack == "blue")
    assert blue.matches(fleet()[0])  # r1
    assert blue.matches(fleet()[8])  # r2


def test_protected_excepted_disallowed_and_disabled_are_never_planned() -> None:
    targets = [
        Target("a", "pay", "prod", "r1", "blue", protected=True),
        Target("b", "pay", "prod", "r1", "test"),
        Target("c", "pay", "other", "r1", "blue"),
    ]
    config = app(exceptions=(ExceptionRule(stack="test"),))
    assert discover_groups([config], targets, ("prod",))["pay"] == []
    assert discover_groups([app(enabled=False)], fleet(), ("prod",)) == {}


def test_group_key_round_trips_through_text() -> None:
    for grouping in Grouping:
        for independent in (True, False):
            key = GroupKey.for_target(Target("x", "pay", "prod", "r1"), grouping, independent)
            assert GroupKey.parse(str(key)) == key
