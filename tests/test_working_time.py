from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from chaosmonkey.working_time import WorkingTime


def london() -> WorkingTime:
    return WorkingTime(
        zone=ZoneInfo("Europe/London"),
        start_hour=9,
        end_hour=15,
        working_days=frozenset(range(5)),
        holidays=frozenset({date(2026, 12, 25)}),
    )


def test_window_follows_local_time_across_daylight_saving() -> None:
    summer_start, _ = london().window(date(2026, 7, 1))
    winter_start, _ = london().window(date(2026, 12, 1))
    assert summer_start == datetime(2026, 7, 1, 8, tzinfo=UTC)  # BST is UTC+1
    assert winter_start == datetime(2026, 12, 1, 9, tzinfo=UTC)


def test_why_closed_explains_each_case() -> None:
    wt = london()
    assert wt.why_closed(datetime(2026, 12, 1, 10, tzinfo=UTC)) is None
    assert "holiday" in (wt.why_closed(datetime(2026, 12, 25, 10, tzinfo=UTC)) or "")
    assert "Saturday" in (wt.why_closed(datetime(2026, 12, 5, 10, tzinfo=UTC)) or "")
    assert "Outside working hours" in (wt.why_closed(datetime(2026, 12, 1, 15, tzinfo=UTC)) or "")


def test_working_days_between_skips_weekends_and_holidays() -> None:
    wt = london()
    friday, monday = date(2026, 12, 18), date(2026, 12, 21)
    assert wt.working_days_between(friday, monday) == 1
    assert wt.working_days_between(friday, friday) == 0
    # 24 Dec (Thu) to 28 Dec (Mon): 25th is a holiday, 26-27 a weekend.
    assert wt.working_days_between(date(2026, 12, 24), date(2026, 12, 28)) == 1


def test_end_hour_24_means_midnight() -> None:
    wt = WorkingTime(ZoneInfo("UTC"), 0, 24, frozenset(range(7)), frozenset())
    start, end = wt.window(date(2026, 11, 9))
    assert (end - start).total_seconds() == 24 * 3600
