"""Limit on parallel LLM requests that can change at runtime (admin setting)."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

# Seconds a waiting request sleeps before it re-reads the limit (it may have been raised).
RECHECK_INTERVAL = 5.0


class DynamicLimiter:
    """At most ``limit()`` holders at a time; the limit is read on every acquire."""

    def __init__(self, limit: Callable[[], Awaitable[int]]) -> None:
        self._limit = limit
        self._active = 0
        self._changed = asyncio.Condition()

    @property
    def active(self) -> int:
        return self._active

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        while True:
            limit = max(1, await self._limit())
            async with self._changed:
                if self._active < limit:
                    self._active += 1
                    break
                try:
                    async with asyncio.timeout(RECHECK_INTERVAL):
                        await self._changed.wait()
                except TimeoutError:
                    pass
        try:
            yield
        finally:
            async with self._changed:
                self._active -= 1
                self._changed.notify_all()
