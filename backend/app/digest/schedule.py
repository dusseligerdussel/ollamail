"""When a digest is due: local delivery time, weekdays and time zone to UTC instants.

Pure functions without I/O. Daylight saving time follows PEP 495 (``fold=0``):

* A local time that does not exist (spring forward, e.g. 02:30 in Berlin on the last
  Sunday of March) is read with the offset *before* the change, so the digest runs one
  hour later by the wall clock (03:30 summer time) instead of being skipped.
* A local time that exists twice (fall back, 02:30 on the last Sunday of October) means its
  first occurrence; the slot still runs once.

All other days run at the configured wall-clock time in both summer and winter time.
"""

from collections.abc import Collection
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

# A slot is searched for in the last/next eight days (one week plus today).
_SEARCH_DAYS = 8


def slot_on(day: date, delivery_time: time, tz: ZoneInfo) -> datetime:
    """The UTC instant of ``delivery_time`` on ``day`` in ``tz``."""
    return datetime.combine(day, delivery_time, tzinfo=tz).astimezone(UTC)


def latest_slot(
    now: datetime, delivery_time: time, tz: ZoneInfo, weekdays: Collection[int]
) -> datetime | None:
    """The most recent slot at or before ``now`` (UTC), ``None`` without weekdays."""
    today = now.astimezone(tz).date()
    for offset in range(_SEARCH_DAYS):
        day = today - timedelta(days=offset)
        if day.weekday() not in weekdays:
            continue
        slot = slot_on(day, delivery_time, tz)
        if slot <= now:
            return slot
    return None


def next_slot(
    now: datetime, delivery_time: time, tz: ZoneInfo, weekdays: Collection[int]
) -> datetime | None:
    """The first slot after ``now`` (UTC), ``None`` without weekdays."""
    today = now.astimezone(tz).date()
    for offset in range(_SEARCH_DAYS):
        day = today + timedelta(days=offset)
        if day.weekday() not in weekdays:
            continue
        slot = slot_on(day, delivery_time, tz)
        if slot > now:
            return slot
    return None


def due_slot(
    now: datetime,
    delivery_time: time,
    tz: ZoneInfo,
    weekdays: Collection[int],
    last_scheduled_for: datetime | None,
) -> datetime | None:
    """The slot to queue now: the latest one, unless it was already queued."""
    slot = latest_slot(now, delivery_time, tz, weekdays)
    if slot is None or (last_scheduled_for is not None and slot <= last_scheduled_for):
        return None
    return slot


def period_start(
    period_end: datetime,
    previous_end: datetime | None,
    *,
    first_lookback: timedelta,
    max_lookback: timedelta,
) -> datetime:
    """Start of a digest's period: the end of the previous digest, bounded by
    ``max_lookback``; ``first_lookback`` before the end for a user's first digest."""
    earliest = period_end - max_lookback
    if previous_end is None:
        return max(period_end - first_lookback, earliest)
    return min(max(previous_end, earliest), period_end)
