"""``LLMConfigResolver`` backed by the admin settings in the database.

Each process caches one snapshot of the settings. It is dropped when another process
announces a change (``LISTEN`` on ``store.CHANNEL``) and, as a safety net for lost
notifications, after ``ttl`` seconds. So a new model applies to the next job without a
restart, in the API and in every worker.

If the database cannot be read, the last snapshot stays in use. Without one, the
environment applies with cloud endpoints blocked (fail closed: a cloud switch turned off
in the admin UI must not be bypassed by an outage).
"""

import asyncio
import time
from collections.abc import Callable

import asyncpg  # type: ignore[import-untyped]
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.llm.config import AIOverrides, ModelAssignment, ResolvedConfig
from app.ai.llm.types import LLMTask
from app.ai.settings.store import CHANNEL, load_overrides
from app.core.config import LLMSettings
from app.core.crypto import CryptoError
from app.core.logging import get_logger

log = get_logger(__name__)

# Seconds a snapshot is used at most (also when no notification arrives).
DEFAULT_TTL = 30.0
# Seconds between pings of the listening connection (detects silent network failures).
KEEPALIVE = 30.0


class DbConfigResolver:
    def __init__(
        self,
        settings: LLMSettings,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        dsn: str | None = None,
        ttl: float = DEFAULT_TTL,
        connect_timeout: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
        autostart: bool = False,
    ) -> None:
        """``dsn`` (libpq URL) enables the change listener, started by ``start`` (or on
        first use with ``autostart``); without it only ``ttl`` and ``invalidate``
        refresh the snapshot."""
        self._autostart = autostart
        self._settings = settings
        self._sessionmaker = sessionmaker
        self._dsn = dsn
        self._ttl = ttl
        self._connect_timeout = connect_timeout
        self._clock = clock
        self._cached: tuple[float, ResolvedConfig] | None = None
        self._generation = 0
        self._lock = asyncio.Lock()
        self._listener: asyncio.Task[None] | None = None

    def invalidate(self) -> None:
        """Drop the snapshot; the next call reads the database."""
        self._cached = None
        self._generation += 1

    async def config(self) -> ResolvedConfig:
        """The current effective configuration."""
        if self._autostart:
            self.start()
        cached = self._cached
        if cached is not None and self._clock() - cached[0] < self._ttl:
            return cached[1]
        async with self._lock:
            cached = self._cached
            if cached is not None and self._clock() - cached[0] < self._ttl:
                return cached[1]
            generation = self._generation
            try:
                async with self._sessionmaker() as session:
                    overrides = await load_overrides(session, self._settings.timeout)
            except (OSError, TimeoutError, SQLAlchemyError, CryptoError) as exc:
                log.warning("ai_settings_unavailable", error_type=type(exc).__name__)
                if cached is not None:
                    return cached[1]
                return ResolvedConfig(self._settings, AIOverrides(cloud_enabled=False))
            config = ResolvedConfig(self._settings, overrides)
            # A change announced while loading may not be in this snapshot.
            if generation == self._generation:
                self._cached = (self._clock(), config)
            return config

    async def resolve(self, task: LLMTask) -> ModelAssignment:
        return (await self.config()).assignment(task)

    async def cloud_allowed(self) -> bool:
        return (await self.config()).cloud_enabled

    async def structured_output_retries(self) -> int:
        return self._settings.structured_output_retries

    async def concurrency(self) -> int:
        """Parallel LLM requests per worker process."""
        return (await self.config()).concurrency

    def start(self) -> None:
        """Start listening for changes (needs a running event loop)."""
        if self._dsn is None or (self._listener is not None and not self._listener.done()):
            return
        self._listener = asyncio.create_task(self._listen_forever(), name="ai-settings-listener")

    async def _listen_forever(self) -> None:
        backoff = 1.0
        while True:
            try:
                await self._listen_once()
                backoff = 1.0
            except (OSError, TimeoutError, asyncpg.PostgresError) as exc:
                log.warning("ai_settings_listener_failed", error_type=type(exc).__name__)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _listen_once(self) -> None:
        assert self._dsn is not None
        connection = await asyncpg.connect(self._dsn, timeout=self._connect_timeout)
        lost = asyncio.Event()
        connection.add_termination_listener(lambda _: lost.set())
        try:
            await connection.add_listener(CHANNEL, lambda *_: self.invalidate())
            # Changes may have been missed while the listener was down.
            self.invalidate()
            while not lost.is_set():
                try:
                    async with asyncio.timeout(KEEPALIVE):
                        await lost.wait()
                except TimeoutError:
                    async with asyncio.timeout(self._connect_timeout):
                        await connection.execute("SELECT 1")
        finally:
            if not connection.is_closed():
                await connection.close(timeout=self._connect_timeout)

    async def aclose(self) -> None:
        if self._listener is not None:
            self._listener.cancel()
            await asyncio.gather(self._listener, return_exceptions=True)
            self._listener = None
