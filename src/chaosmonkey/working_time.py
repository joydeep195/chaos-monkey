"""Working days and hours in the configured time zone.

Netflix's Chaos Monkey kills only on weekdays, inside working hours, so that engineers are at
their desks when something goes wrong. This version adds a holiday list.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from chaosmonkey.settings import WEEKDAY_NUMBERS, Settings


@dataclass(frozen=True)
class WorkingTime:
    zone: ZoneInfo
    start_hour: int
    end_hour: int
    working_days: frozenset[int]
    holidays: frozenset[date]

    @classmethod
    def from_settings(cls, settings: Settings) -> WorkingTime:
        return cls(
            zone=settings.zone,
            start_hour=settings.start_hour,
            end_hour=settings.end_hour,
            working_days=frozenset(WEEKDAY_NUMBERS[day] for day in settings.working_days),
            holidays=frozenset(settings.holidays),
        )

    def local_date(self, moment: datetime) -> date:
        return moment.astimezone(self.zone).date()

    def is_working_day(self, day: date) -> bool:
        return day.weekday() in self.working_days and day not in self.holidays

    def window(self, day: date) -> tuple[datetime, datetime]:
        """Start and end of working hours on this day, in UTC."""
        midnight = datetime(day.year, day.month, day.day, tzinfo=self.zone)
        start = (midnight + timedelta(hours=self.start_hour)).astimezone(UTC)
        end = (midnight + timedelta(hours=self.end_hour)).astimezone(UTC)
        return start, end

    def day_bounds(self, day: date) -> tuple[datetime, datetime]:
        """Start and end of the whole local day, in UTC."""
        midnight = datetime(day.year, day.month, day.day, tzinfo=self.zone)
        following = midnight.date() + timedelta(days=1)
        next_midnight = datetime(following.year, following.month, following.day, tzinfo=self.zone)
        return midnight.astimezone(UTC), next_midnight.astimezone(UTC)

    def why_closed(self, moment: datetime) -> str | None:
        """None inside working hours; otherwise a plain reason."""
        day = self.local_date(moment)
        if day in self.holidays:
            return f"{day.isoformat()} is a holiday"
        if day.weekday() not in self.working_days:
            return f"{day.strftime('%A')} is not a working day"
        start, end = self.window(day)
        if not start <= moment < end:
            return (
                f"Outside working hours ({self.start_hour:02d}:00 to {self.end_hour:02d}:00 "
                f"{self.zone.key})"
            )
        return None

    def working_days_between(self, earlier: date, later: date) -> int:
        """How many working days fall after `earlier`, up to and including `later`."""
        count = 0
        day = earlier + timedelta(days=1)
        while day <= later:
            if self.is_working_day(day):
                count += 1
            day += timedelta(days=1)
        return count
