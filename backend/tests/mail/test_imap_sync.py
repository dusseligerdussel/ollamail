"""End to end: IMAP server → sync engine → database (markers ``db`` and ``imap``).

Covers the acceptance criteria of #14: an initial import of 1,000 messages that resumes
after an interruption, and new mail reaching the database via IDLE in under 10 seconds.
"""

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import MailSettings
from app.mail import hooks
from app.mail.models import Message, SyncState
from app.mail.providers.base import (
    ConnectionFailedError,
    MailboxConfig,
    MessageFetched,
    SyncCursor,
    SyncEvent,
)
from app.mail.providers.imap import ImapProvider
from app.mail.providers.imap_client import Literal_
from app.mail.storage import AttachmentStorage
from app.mail.sync.engine import sync_mailbox
from app.mail.sync.watcher import MailboxWatcher
from tests.mail.conftest import add_mailbox, dsn, eventually
from tests.mail.imap_server import TestAccount, message

pytestmark = [pytest.mark.db, pytest.mark.imap]

SETTINGS = MailSettings(sync_batch_size=100)


class Interrupted:
    """``ImapProvider`` whose import breaks off after ``limit`` fetched messages."""

    def __init__(self, provider: ImapProvider, limit: int) -> None:
        self.provider = provider
        self.capabilities = provider.capabilities
        self.limit = limit

    async def list_folders(self) -> Any:
        return await self.provider.list_folders()

    async def fetch_since(
        self, folder_id: str, cursor: SyncCursor | None, *, since: datetime | None = None
    ) -> AsyncIterator[SyncEvent]:
        fetched = 0
        async for event in self.provider.fetch_since(folder_id, cursor, since=since):
            if isinstance(event, MessageFetched):
                if fetched == self.limit:
                    raise ConnectionFailedError()
                fetched += 1
            yield event

    async def aclose(self) -> None:
        await self.provider.aclose()


@pytest.fixture
def stored() -> Iterator[list[uuid.UUID]]:
    calls: list[uuid.UUID] = []

    async def record(event: hooks.MessageStored) -> None:
        calls.append(event.message_id)

    hooks.on_message_stored(record)
    yield calls
    hooks.remove_message_stored_handler(record)


async def count_messages(factory: async_sessionmaker[AsyncSession], mailbox_id: uuid.UUID) -> int:
    async with factory() as session:
        query = select(func.count()).select_from(Message).where(Message.mailbox_id == mailbox_id)
        return await session.scalar(query) or 0


async def test_initial_import_of_1000_messages_resumes_after_interruption(
    imap_account: TestAccount,
    sessionmaker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    stored: list[uuid.UUID],
) -> None:
    for start in range(0, 1000, 100):
        parts: list[Any] = ["APPEND", b"INBOX"]
        for number in range(start, start + 100):
            parts += ["()", Literal_(message(number))]
        conn = await imap_account.admin()
        await conn.command(*parts)  # MULTIAPPEND
    mailbox_id = await add_mailbox(sessionmaker, address=imap_account.address)
    storage = AttachmentStorage(tmp_path)

    async def sync(provider: Any) -> Any:
        async with sessionmaker() as session:
            return await sync_mailbox(
                session,
                mailbox_id,
                storage=storage,
                settings=SETTINGS,
                provider_factory=lambda config: provider,
            )

    with pytest.raises(ConnectionFailedError):
        await sync(Interrupted(imap_account.provider(batch_size=100), limit=350))
    # Three complete batches were committed; the fourth was rolled back.
    assert await count_messages(sessionmaker, mailbox_id) == 300
    assert len(stored) == 300

    stats = await sync(imap_account.provider(batch_size=100))

    assert stats is not None and stats.stored == 700
    assert await count_messages(sessionmaker, mailbox_id) == 1000
    # Every message was announced exactly once.
    assert len(stored) == len(set(stored)) == 1000
    async with sessionmaker() as session:
        cursors: list[dict[str, Any]] = list(
            await session.scalars(
                select(SyncState.cursor).where(
                    SyncState.mailbox_id == mailbox_id, SyncState.folder_id.is_not(None)
                )
            )
        )
    assert {"known": "1:1000"}.items() <= next(c for c in cursors if c.get("known")).items()


async def test_new_mail_reaches_the_database_via_idle_in_under_10_seconds(
    imap_account: TestAccount,
    sessionmaker: async_sessionmaker[AsyncSession],
    migrated_database: str,
    tmp_path: Path,
) -> None:
    mailbox_id = await add_mailbox(sessionmaker, address=imap_account.address)
    storage = AttachmentStorage(tmp_path)
    syncs: list[float] = []
    lock = asyncio.Lock()

    def provider_factory(config: MailboxConfig) -> ImapProvider:
        return imap_account.provider()

    async def request_sync(requested: uuid.UUID) -> None:
        # Stands in for the job queue: run the sync right away, one at a time.
        async with lock, sessionmaker() as session:
            await sync_mailbox(
                session,
                requested,
                storage=storage,
                settings=SETTINGS,
                provider_factory=provider_factory,
            )
        syncs.append(time.monotonic())

    watcher = MailboxWatcher(
        sessionmaker=sessionmaker,
        dsn=dsn(migrated_database),
        request_sync=request_sync,
        provider_factory=provider_factory,
        refresh_interval=0.2,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(watcher.run(stop))
    try:
        await eventually(lambda: len(syncs) == 1, seconds=10)  # initial sync
        await asyncio.sleep(1)  # IDLE is running
        started = time.monotonic()
        await imap_account.append(message(1, subject="Neue Nachricht"))
        for _ in range(100):
            if await count_messages(sessionmaker, mailbox_id) == 1:
                break
            await asyncio.sleep(0.1)
        elapsed = time.monotonic() - started
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=10)

    assert await count_messages(sessionmaker, mailbox_id) == 1
    assert elapsed < 10
