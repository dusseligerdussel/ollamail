"""Identifier helpers."""

import os
import time
import uuid
from datetime import UTC, datetime

_RAND_A_MASK = (1 << 12) - 1
_RAND_B_MASK = (1 << 62) - 1


def uuid7() -> uuid.UUID:
    """Return a time-ordered UUID version 7 (RFC 9562).

    Layout: 48-bit Unix timestamp in milliseconds, version, 12 random bits, variant,
    62 random bits. IDs created in different milliseconds sort by creation time, which
    keeps B-tree indexes compact. Python 3.14 ships ``uuid.uuid7``; this can be replaced
    once 3.12 support is dropped.
    """
    timestamp_ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")
    value = (
        (timestamp_ms & ((1 << 48) - 1)) << 80
        | 0x7 << 76
        | ((rand >> 62) & _RAND_A_MASK) << 64
        | 0b10 << 62
        | (rand & _RAND_B_MASK)
    )
    return uuid.UUID(int=value)


def uuid7_floor(moment: datetime) -> uuid.UUID:
    """Smallest UUIDv7 of ``moment``'s millisecond: every ``uuid7()`` created at or after
    ``moment`` sorts at or above it (range scans over time-ordered primary keys)."""
    timestamp_ms = max(0, int(moment.timestamp() * 1000))
    return uuid.UUID(int=(timestamp_ms & ((1 << 48) - 1)) << 80 | 0x7 << 76 | 0b10 << 62)


def uuid7_time(value: uuid.UUID) -> datetime:
    """Creation time of a UUIDv7 (millisecond precision)."""
    return datetime.fromtimestamp((value.int >> 80) / 1000, tz=UTC)
