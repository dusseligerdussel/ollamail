"""Slots, weekdays, time zones and daylight saving time (unit tests, no database)."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.digest.schedule import due_slot, latest_slot, next_slot, period_start

BERLIN = ZoneInfo("Europe/Berlin")
NEW_YORK = ZoneInfo("America/New_York")
EVERY_DAY = range(7)
WORKDAYS = range(5)


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)  # type: ignore[misc]


def test_winter_time_is_utc_plus_one() -> None:
    # Friday, 9 January 2026, 06:30 UTC = 07:30 in Berlin.
    now = utc(2026, 1, 9, 6, 30)

    assert latest_slot(now, time(7, 0), BERLIN, EVERY_DAY) == utc(2026, 1, 9, 6, 0)
    assert next_slot(now, time(7, 0), BERLIN, EVERY_DAY) == utc(2026, 1, 10, 6, 0)


def test_summer_time_is_utc_plus_two() -> None:
    now = utc(2026, 7, 10, 5, 30)

    assert latest_slot(now, time(7, 0), BERLIN, EVERY_DAY) == utc(2026, 7, 10, 5, 0)


def test_slot_keeps_wall_clock_time_across_spring_forward() -> None:
    # Clocks go from 02:00 to 03:00 on Sunday, 29 March 2026.
    saturday = latest_slot(utc(2026, 3, 28, 12), time(7, 0), BERLIN, EVERY_DAY)
    sunday = latest_slot(utc(2026, 3, 29, 12), time(7, 0), BERLIN, EVERY_DAY)

    assert saturday == utc(2026, 3, 28, 6, 0)
    assert sunday == utc(2026, 3, 29, 5, 0)
    # Only 23 hours apart, and both at 07:00 local time.
    assert sunday - saturday == timedelta(hours=23)
    assert sunday.astimezone(BERLIN).time() == time(7, 0)


def test_slot_keeps_wall_clock_time_across_fall_back() -> None:
    # Clocks go from 03:00 back to 02:00 on Sunday, 25 October 2026.
    saturday = latest_slot(utc(2026, 10, 24, 12), time(7, 0), BERLIN, EVERY_DAY)
    sunday = latest_slot(utc(2026, 10, 25, 12), time(7, 0), BERLIN, EVERY_DAY)

    assert sunday - saturday == timedelta(hours=25)
    assert sunday.astimezone(BERLIN).time() == time(7, 0)


def test_nonexistent_local_time_runs_an_hour_later() -> None:
    # 02:30 does not exist on 29 March 2026: the slot is 01:30 UTC (03:30 summer time).
    slot = latest_slot(utc(2026, 3, 29, 12), time(2, 30), BERLIN, EVERY_DAY)

    assert slot == utc(2026, 3, 29, 1, 30)
    assert slot.astimezone(BERLIN).time() == time(3, 30)


def test_ambiguous_local_time_runs_once_at_its_first_occurrence() -> None:
    # 02:30 exists twice on 25 October 2026 (00:30 and 01:30 UTC).
    first = utc(2026, 10, 25, 0, 30)
    slot = latest_slot(utc(2026, 10, 25, 1, 45), time(2, 30), BERLIN, EVERY_DAY)

    assert slot == first
    # The second 02:30 does not queue the slot again.
    assert due_slot(utc(2026, 10, 25, 1, 31), time(2, 30), BERLIN, EVERY_DAY, first) is None


def test_scheduler_queues_every_slot_exactly_once_over_dst_changes() -> None:
    """Simulate the minute scheduler over spring forward and fall back."""
    for start in (utc(2026, 3, 27), utc(2026, 10, 23)):
        # As after enabling the digest: the past slot does not fire.
        last = latest_slot(start, time(2, 30), BERLIN, EVERY_DAY)
        slots = []
        now = start
        while now < start + timedelta(days=4):
            slot = due_slot(now, time(2, 30), BERLIN, EVERY_DAY, last)
            if slot is not None:
                slots.append(slot)
                last = slot
            now += timedelta(minutes=15)
        # One slot per local day, none skipped, none doubled.
        days = [slot.astimezone(BERLIN).date() for slot in slots]
        assert len(days) == len(set(days)) == 4
        assert days == sorted(days)


def test_other_time_zones_follow_their_own_dst_dates() -> None:
    # New York switches on 8 March 2026, three weeks before Europe.
    before = latest_slot(utc(2026, 3, 7, 18), time(7, 0), NEW_YORK, EVERY_DAY)
    after = latest_slot(utc(2026, 3, 9, 18), time(7, 0), NEW_YORK, EVERY_DAY)

    assert before == utc(2026, 3, 7, 12, 0)
    assert after == utc(2026, 3, 9, 11, 0)


def test_weekdays_skip_the_weekend() -> None:
    # Saturday, 3 October 2026, noon: the last workday slot was Friday.
    now = utc(2026, 10, 3, 12)

    assert latest_slot(now, time(7, 0), BERLIN, WORKDAYS) == utc(2026, 10, 2, 5, 0)
    assert next_slot(now, time(7, 0), BERLIN, WORKDAYS) == utc(2026, 10, 5, 5, 0)


def test_no_weekdays_means_no_slot() -> None:
    now = utc(2026, 10, 3, 12)

    assert latest_slot(now, time(7, 0), BERLIN, []) is None
    assert next_slot(now, time(7, 0), BERLIN, []) is None


def test_local_date_decides_the_weekday() -> None:
    # Sunday 23:30 UTC is already Monday 01:30 in Berlin.
    now = utc(2026, 10, 4, 23, 30)

    assert latest_slot(now, time(1, 0), BERLIN, [0]) == utc(2026, 10, 4, 23, 0)


def test_due_slot_only_once() -> None:
    now = utc(2026, 10, 2, 5, 1)
    slot = due_slot(now, time(7, 0), BERLIN, EVERY_DAY, None)

    assert slot == utc(2026, 10, 2, 5, 0)
    assert due_slot(now, time(7, 0), BERLIN, EVERY_DAY, slot) is None
    assert due_slot(utc(2026, 10, 3, 5, 0), time(7, 0), BERLIN, EVERY_DAY, slot) == utc(
        2026, 10, 3, 5, 0
    )


def test_period_continues_from_the_previous_digest() -> None:
    end = utc(2026, 10, 2, 5)
    kwargs = {"first_lookback": timedelta(hours=24), "max_lookback": timedelta(days=7)}

    assert period_start(end, None, **kwargs) == utc(2026, 10, 1, 5)
    assert period_start(end, utc(2026, 9, 30, 5), **kwargs) == utc(2026, 9, 30, 5)
    # After a long pause, at most the maximum.
    assert period_start(end, utc(2026, 8, 1), **kwargs) == utc(2026, 9, 25, 5)
    # Never after the end.
    assert period_start(end, utc(2026, 10, 3), **kwargs) == end
