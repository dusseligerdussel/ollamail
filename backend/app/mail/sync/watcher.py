"""Push watcher: one long-lived server connection per mailbox (IMAP ``IDLE``) that queues
syncs as soon as the server reports changes.

The watcher runs inside the worker process, next to the job workers (``app.worker.run``),
when the process consumes the ``sync`` queue. It is deliberately not a job: a job per
mailbox would occupy a worker slot forever and would stay "doing" after a crash.

Every mailbox is watched by exactly one process: a process holds a PostgreSQL advisory
lock per watched mailbox on its own lock connection. If the process dies, the connection
and its locks go away and another worker picks the mailboxes up on its next refresh.

Per mailbox:

* on start and after every reconnect a sync is requested (changes missed while
  disconnected),
* every push event requests a sync (``request_sync`` de-duplicates queued jobs),
* independent of push, a sync is requested every ``poll_interval_seconds`` (other folders
  than INBOX, servers without ``IDLE``),
* errors reconnect with exponential backoff (1 s up to 5 min; 30 min after
  authentication or configuration errors).
"""

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import asyncpg  # type: ignore[import-untyped]
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.db import libpq_url, process_database
from app.core.logging import get_logger
from app.mail.models import Mailbox
from app.mail.providers.base import (
    AuthenticationError,
    ConfigurationError,
    ConnectionFailedError,
    MailboxConfig,
    MailProvider,
    ProviderError,
)
from app.mail.providers.registry import ProviderFactory, registry
from app.mail.schemas import SyncSettings
from app.mail.sync.engine import credentials_saver, mailbox_config

log = get_logger(__name__)

# First key of the advisory locks (``pg_try_advisory_lock(namespace, hashtext(id))``).
LOCK_NAMESPACE = 0x6F6D6C
# A watch that ran this long counts as healthy: the backoff starts again at 1 s.
_HEALTHY_SECONDS = 60.0

RequestSync = Callable[[uuid.UUID], Awaitable[object]]


@dataclass(slots=True)
class _Watch:
    task: asyncio.Task[None]
    poll_interval: float


class MailboxWatcher:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        dsn: str,
        request_sync: RequestSync,
        provider_factory: ProviderFactory = registry.create,
        refresh_interval: float = 60.0,
        max_backoff: float = 300.0,
        error_backoff: float = 1800.0,
        connect_timeout: float = 5.0,
        poll_interval: float | None = None,
    ) -> None:
        """``poll_interval`` overrides the mailboxes' ``poll_interval_seconds`` (tests)."""
        self._sessionmaker = sessionmaker
        self._dsn = dsn
        self._request_sync = request_sync
        self._provider_factory = provider_factory
        self._refresh_interval = refresh_interval
        self._max_backoff = max_backoff
        self._error_backoff = error_backoff
        self._connect_timeout = connect_timeout
        self._poll_interval = poll_interval
        self._watches: dict[uuid.UUID, _Watch] = {}

    @property
    def watched(self) -> frozenset[uuid.UUID]:
        return frozenset(self._watches)

    async def run(self, stop: asyncio.Event) -> None:
        """Watch all enabled mailboxes until ``stop`` is set."""
        backoff = 1.0
        while not stop.is_set():
            try:
                connection = await asyncpg.connect(self._dsn, timeout=self._connect_timeout)
            except (OSError, TimeoutError, asyncpg.PostgresError) as exc:
                log.warning("mail_watcher_db_unavailable", error_type=type(exc).__name__)
                await _wait(stop, backoff)
                backoff = min(backoff * 2, 60.0)
                continue
            lost = asyncio.Event()
            connection.add_termination_listener(lambda _, lost=lost: lost.set())
            try:
                while not stop.is_set() and not lost.is_set():
                    await self._reconcile(connection)
                    backoff = 1.0
                    await _wait_any(stop, lost, self._refresh_interval)
            except Exception as exc:
                # Database errors (also wrapped by SQLAlchemy) must not end the watcher.
                log.warning("mail_watcher_failed", error_type=type(exc).__name__)
            finally:
                await self._stop_all()
                if not connection.is_closed():
                    # Closing the session releases all advisory locks.
                    with contextlib.suppress(Exception):
                        await connection.close(timeout=self._connect_timeout)
            if not stop.is_set():
                await _wait(stop, backoff)
                backoff = min(backoff * 2, 60.0)

    async def _reconcile(self, connection: asyncpg.Connection) -> None:
        async with self._sessionmaker() as session:
            rows = (
                await session.execute(
                    select(Mailbox.id, Mailbox.sync_settings).where(Mailbox.sync_enabled)
                )
            ).all()
        wanted = {
            mailbox_id: self._poll_interval
            or SyncSettings.model_validate(settings or {}).poll_interval_seconds
            for mailbox_id, settings in rows
        }
        for mailbox_id in [m for m in self._watches if m not in wanted]:
            await self._cancel(self._watches.pop(mailbox_id))
            await connection.execute(
                "SELECT pg_advisory_unlock($1, hashtext($2))", LOCK_NAMESPACE, str(mailbox_id)
            )
            log.info("mail_watch_stopped", mailbox_id=str(mailbox_id))

        for mailbox_id, poll_interval in wanted.items():
            current = self._watches.get(mailbox_id)
            if current is None:
                locked = await connection.fetchval(
                    "SELECT pg_try_advisory_lock($1, hashtext($2))",
                    LOCK_NAMESPACE,
                    str(mailbox_id),
                )
                if not locked:
                    continue
                log.info("mail_watch_started", mailbox_id=str(mailbox_id))
            elif not current.task.done() and current.poll_interval == poll_interval:
                continue
            else:
                await self._cancel(current)
            task = asyncio.create_task(
                self._watch(mailbox_id, poll_interval), name=f"mail-watch-{mailbox_id}"
            )
            self._watches[mailbox_id] = _Watch(task, poll_interval)

    async def _cancel(self, watch: _Watch) -> None:
        watch.task.cancel()
        await asyncio.gather(watch.task, return_exceptions=True)

    async def _stop_all(self) -> None:
        watches, self._watches = list(self._watches.values()), {}
        for watch in watches:
            await self._cancel(watch)

    # -- one mailbox ------------------------------------------------------------------

    async def _config(self, mailbox_id: uuid.UUID) -> MailboxConfig | None:
        async with self._sessionmaker() as session:
            mailbox = await session.get(Mailbox, mailbox_id)
            if mailbox is None or not mailbox.sync_enabled:
                return None
            return mailbox_config(
                mailbox, save_credentials=credentials_saver(self._sessionmaker, mailbox_id)
            )

    async def _request(self, mailbox_id: uuid.UUID) -> None:
        try:
            await self._request_sync(mailbox_id)
        except Exception as exc:
            log.warning(
                "mail_sync_request_failed",
                mailbox_id=str(mailbox_id),
                error_type=type(exc).__name__,
            )

    async def _watch(self, mailbox_id: uuid.UUID, poll_interval: float) -> None:
        loop = asyncio.get_running_loop()
        backoff = 1.0
        while True:
            started = loop.time()
            try:
                config = await self._config(mailbox_id)
                if config is None:
                    return
                provider = self._provider_factory(config)
                try:
                    await self._request(mailbox_id)
                    await self._watch_provider(provider, mailbox_id, poll_interval)
                finally:
                    await provider.aclose()
            except (AuthenticationError, ConfigurationError) as exc:
                log.warning("mail_watch_failed", mailbox_id=str(mailbox_id), error=exc.code)
                await asyncio.sleep(self._error_backoff)
                continue
            except Exception as exc:
                error = exc.code if isinstance(exc, ProviderError) else type(exc).__name__
                log.warning("mail_watch_failed", mailbox_id=str(mailbox_id), error=error)
            if loop.time() - started >= _HEALTHY_SECONDS:
                backoff = 1.0
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self._max_backoff)

    async def _watch_provider(
        self, provider: MailProvider, mailbox_id: uuid.UUID, poll_interval: float
    ) -> None:
        poller = asyncio.create_task(self._poll(mailbox_id, poll_interval))
        try:
            try:
                async for _ in provider.watch(None):
                    await self._request(mailbox_id)
            except NotImplementedError:
                # No push: polling only.
                await poller
            raise ConnectionFailedError()
        finally:
            poller.cancel()
            await asyncio.gather(poller, return_exceptions=True)

    async def _poll(self, mailbox_id: uuid.UUID, poll_interval: float) -> None:
        while True:
            await asyncio.sleep(poll_interval)
            await self._request(mailbox_id)


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    try:
        async with asyncio.timeout(seconds):
            await stop.wait()
    except TimeoutError:
        pass


async def _wait_any(stop: asyncio.Event, lost: asyncio.Event, seconds: float) -> None:
    waiters = [asyncio.create_task(stop.wait()), asyncio.create_task(lost.wait())]
    try:
        await asyncio.wait(waiters, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)


async def run_watcher(settings: Settings, stop: asyncio.Event) -> None:
    """Entry point for the worker: watch mailboxes and queue ``mail.sync_mailbox`` jobs."""
    from app.mail.sync.tasks import request_sync

    # The worker's shared engine; ``app.worker.run`` disposes it on shutdown.
    await MailboxWatcher(
        sessionmaker=process_database().sessionmaker,
        dsn=libpq_url(settings.database),
        request_sync=request_sync,
        connect_timeout=settings.database.connect_timeout,
    ).run(stop)
