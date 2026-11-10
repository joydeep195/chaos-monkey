"""Time source. Code asks a Clock for the time so that tests can control it."""

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Return the current time as a timezone-aware UTC datetime."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """A clock that only moves when told to. Used in tests and rehearsals."""

    def __init__(self, start: datetime) -> None:
        self._now = _require_aware(start)

    def now(self) -> datetime:
        return self._now

    def advance(
        self, *, days: float = 0, hours: float = 0, minutes: float = 0, seconds: float = 0
    ) -> None:
        self._now += timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)

    def set(self, when: datetime) -> None:
        self._now = _require_aware(when)


def _require_aware(when: datetime) -> datetime:
    if when.tzinfo is None:
        raise ValueError("Times must be timezone-aware")
    return when.astimezone(UTC)
