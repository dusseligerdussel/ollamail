"""Deferring background jobs from the API process.

The worker opens the Procrastinate app itself (``app.worker.run``). In the API the app is
opened on first use, so endpoints that never queue jobs (and the API's startup) do not
depend on the job queue, and closed on shutdown.
"""

import asyncio

from app.worker import app as worker_app


class JobQueue:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._open = False

    async def ensure_open(self) -> None:
        async with self._lock:
            if not self._open:
                await worker_app.open_async()
                self._open = True

    async def close(self) -> None:
        async with self._lock:
            if self._open:
                await worker_app.close_async()
                self._open = False
