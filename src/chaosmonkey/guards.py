"""Safety guards. Every one must agree before an instance is terminated.

Each guard is a small function that looks at the situation and returns None (fine)
or a plain-English reason to refuse. The first refusal wins and is recorded in the
audit log, so every skipped kill says exactly why it was skipped.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime

from chaosmonkey.domain import AppConfig, GroupKey, Mode, Target
from chaosmonkey.settings import Settings
from chaosmonkey.working_time import WorkingTime


@dataclass(frozen=True)
class KillSwitch:
    engaged: bool
    reason: str = ""
    by: str = ""
    at: str = ""


@dataclass(frozen=True)
class Situation:
    """Everything the guards need, gathered once just before a kill."""

    now: datetime
    settings: Settings
    working_time: WorkingTime
    app: AppConfig | None
    group: GroupKey
    mode: Mode
    # Eligible targets in the group (protected and opted-out ones already removed).
    targets: list[Target]
    kill_switch: KillSwitch
    open_incident: str | None
    freeze_window: str | None
    last_kill_day: date | None
    live_kills_today: int
    live_kills_recovering: int


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    guard: str
    reason: str


def kill_switch(s: Situation) -> str | None:
    if s.kill_switch.engaged:
        return f"Kill switch is on ({s.kill_switch.reason}, by {s.kill_switch.by})"
    return None


def master_switch(s: Situation) -> str | None:
    if not s.settings.enabled:
        return "Chaos Monkey is switched off in settings (enabled = false)"
    return None


def app_opted_in(s: Situation) -> str | None:
    if s.app is None:
        return f"App {s.group.app} is not registered"
    if not s.app.enabled:
        return f"App {s.app.name} has not opted in"
    return None


def account_allowed(s: Situation) -> str | None:
    if s.group.account not in s.settings.allowed_accounts:
        return f"Account {s.group.account} is not on the allowlist (allowed_accounts)"
    return None


def working_hours(s: Situation) -> str | None:
    return s.working_time.why_closed(s.now)


def freeze_window(s: Situation) -> str | None:
    if s.freeze_window:
        return f"Freeze window in effect: {s.freeze_window}"
    return None


def no_open_incident(s: Situation) -> str | None:
    if s.open_incident:
        return f"An incident is open: {s.open_incident}"
    return None


def minimum_gap(s: Situation) -> str | None:
    if s.app is None or s.last_kill_day is None:
        return None
    today = s.working_time.local_date(s.now)
    gap = s.working_time.working_days_between(s.last_kill_day, today)
    if gap < s.app.min_days_between_kills:
        return (
            f"This group last lost an instance on {s.last_kill_day.isoformat()}; the minimum "
            f"gap is {s.app.min_days_between_kills} working day(s)"
        )
    return None


def daily_cap(s: Situation) -> str | None:
    if s.mode is Mode.LIVE and s.live_kills_today >= s.settings.max_kills_per_day:
        return f"Daily limit reached ({s.settings.max_kills_per_day} live kills)"
    return None


def concurrency_cap(s: Situation) -> str | None:
    if s.mode is Mode.LIVE and s.live_kills_recovering >= s.settings.max_concurrent_kills:
        return (
            f"{s.live_kills_recovering} earlier kill(s) still recovering; the limit is "
            f"{s.settings.max_concurrent_kills} at a time"
        )
    return None


def group_healthy(s: Situation) -> str | None:
    if not s.targets:
        return "No eligible instances in this group"
    unhealthy = sum(1 for t in s.targets if not t.healthy)
    if unhealthy:
        return f"Group is already degraded: {unhealthy} of {len(s.targets)} instances unhealthy"
    return None


def capacity(s: Situation) -> str | None:
    if s.app is None:
        return None
    healthy = sum(1 for t in s.targets if t.healthy)
    if healthy - 1 < s.app.min_healthy:
        return (
            f"Only {healthy} healthy instance(s); one kill would leave {healthy - 1}, below "
            f"the minimum of {s.app.min_healthy}"
        )
    return None


# Order matters only for which reason is reported first: cheapest and broadest first.
GUARDS: tuple[tuple[str, Callable[[Situation], str | None]], ...] = (
    ("kill_switch", kill_switch),
    ("master_switch", master_switch),
    ("app_opted_in", app_opted_in),
    ("account_allowed", account_allowed),
    ("working_hours", working_hours),
    ("freeze_window", freeze_window),
    ("no_open_incident", no_open_incident),
    ("minimum_gap", minimum_gap),
    ("daily_cap", daily_cap),
    ("concurrency_cap", concurrency_cap),
    ("group_healthy", group_healthy),
    ("capacity", capacity),
)


def evaluate(situation: Situation) -> Verdict:
    for name, guard in GUARDS:
        reason = guard(situation)
        if reason is not None:
            return Verdict(False, name, reason)
    return Verdict(True, "all", "All guards passed")
