"""Netflix's scheduling algorithm.

Each working day, for every group of every opted-in app, flip a weighted coin whose
chance of heads is 1 / (mean working days between kills). On heads, plan one kill at
a random moment inside working hours. Over time the gap between kills averages the
chosen mean (a geometric distribution), and no group ever gets more than one kill a
day. Which instance dies is decided at kill time, not here, so the choice reflects
the fleet as it is then.
"""

import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from chaosmonkey.domain import AppConfig, GroupKey, Target, eligible_targets
from chaosmonkey.working_time import WorkingTime


@dataclass(frozen=True)
class PlannedKill:
    app: str
    group: GroupKey
    run_at: datetime


def discover_groups(
    apps: list[AppConfig], targets: list[Target], allowed_accounts: tuple[str, ...]
) -> dict[str, list[GroupKey]]:
    """Groups of each enabled app that currently contain at least one eligible target."""
    groups: dict[str, list[GroupKey]] = {}
    for app in apps:
        if not app.enabled:
            continue
        keys = {
            GroupKey.for_target(t, app.grouping, app.regions_independent)
            for t in eligible_targets(app, targets)
            if t.account in allowed_accounts
        }
        groups[app.name] = sorted(keys)
    return groups


def plan_day(
    day: date,
    *,
    apps: list[AppConfig],
    targets: list[Target],
    allowed_accounts: tuple[str, ...],
    working_time: WorkingTime,
    rng: random.Random,
    not_before: datetime | None = None,
) -> list[PlannedKill]:
    """The kills for one day. Planning late in the day only uses the time that is left."""
    if not working_time.is_working_day(day):
        return []
    start, end = working_time.window(day)
    if not_before is not None:
        start = max(start, not_before)
    if start >= end:
        return []
    seconds = (end - start).total_seconds()

    by_name = {app.name: app for app in apps}
    planned: list[PlannedKill] = []
    for app_name, groups in sorted(discover_groups(apps, targets, allowed_accounts).items()):
        chance = 1.0 / by_name[app_name].mean_days_between_kills
        for group in groups:
            if rng.random() < chance:
                run_at = start + timedelta(seconds=rng.random() * seconds)
                planned.append(PlannedKill(app_name, group, run_at))
    return sorted(planned, key=lambda kill: kill.run_at)
