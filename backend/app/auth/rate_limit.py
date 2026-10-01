"""Fixed-window counters in PostgreSQL (no Redis, docs/ARCHITECTURE.md §1).

``hit`` increments atomically (``INSERT … ON CONFLICT DO UPDATE``) and returns the count
of the current window, so concurrent requests cannot slip past a limit. Callers commit
right after a hit to release the row lock before slow work such as password hashing.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import rate_limits


@dataclass(frozen=True)
class Hit:
    count: int
    window_start: datetime
    window: timedelta

    def retry_after(self, now: datetime | None = None) -> int:
        """Seconds until the window ends (at least 1)."""
        remaining = self.window_start + self.window - (now or datetime.now(UTC))
        return max(1, int(remaining.total_seconds()) + 1)


async def hit(session: AsyncSession, key: str, window: timedelta) -> Hit:
    now = datetime.now(UTC)
    table = rate_limits.c
    expired = table.window_start <= now - window
    upsert = insert(rate_limits).values(key=key, window_start=now, hits=1)
    statement = upsert.on_conflict_do_update(
        index_elements=[table.key],
        set_={
            "window_start": case((expired, now), else_=table.window_start),
            "hits": case((expired, 1), else_=table.hits + 1),
        },
    ).returning(table.hits, table.window_start)
    row = (await session.execute(statement)).one()
    return Hit(count=row.hits, window_start=row.window_start, window=window)


async def reset(session: AsyncSession, key: str) -> None:
    await session.execute(delete(rate_limits).where(rate_limits.c.key == key))


async def purge(session: AsyncSession, older_than: timedelta) -> int:
    """Delete counters whose window started before ``now - older_than``."""
    cutoff = datetime.now(UTC) - older_than
    result = await session.execute(delete(rate_limits).where(rate_limits.c.window_start < cutoff))
    return int(getattr(result, "rowcount", 0) or 0)
