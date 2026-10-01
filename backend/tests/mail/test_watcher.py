"""``MailboxWatcher`` with ``FakeMailProvider``. The watcher uses its own sessions and a lock
connection, so the mailboxes are committed and removed again after each test."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.mail.models import FolderRole, Mailbox
from app.mail.providers.base import AuthenticationError, MailboxConfig, RemoteFolder
from app.mail.providers.fake import FakeMailProvider
from app.mail.sync.watcher import MailboxWatcher
from tests.mail.conftest import add_mailbox, dsn, eventually

pytestmark = pytest.mark.db


async def set_enabled(
    factory: async_sessionmaker[AsyncSession], mailbox_id: uuid.UUID, enabled: bool
) -> None:
    async with factory() as session:
        await session.execute(
            update(Mailbox).where(Mailbox.id == mailbox_id).values(sync_enabled=enabled)
        )
        await session.commit()


class Requests:
    def __init__(self) -> None:
        self.calls: list[uuid.UUID] = []

    async def __call__(self, mailbox_id: uuid.UUID) -> None:
        self.calls.append(mailbox_id)

    def count(self, mailbox_id: uuid.UUID) -> int:
        return self.calls.count(mailbox_id)


def make_watcher(
    factory: async_sessionmaker[AsyncSession],
    url: str,
    requests: Callable[[uuid.UUID], Awaitable[object]],
    provider_factory: Callable[[MailboxConfig], Any],
    **kwargs: Any,
) -> MailboxWatcher:
    return MailboxWatcher(
        sessionmaker=factory,
        dsn=dsn(url),
        request_sync=requests,
        provider_factory=provider_factory,
        refresh_interval=0.1,
        **kwargs,
    )


async def test_push_events_request_syncs(
    sessionmaker: async_sessionmaker[AsyncSession], migrated_database: str
) -> None:
    fake = FakeMailProvider()
    fake.add_folder(RemoteFolder("INBOX", "Inbox", role=FolderRole.INBOX))
    mailbox_id = await add_mailbox(sessionmaker)
    disabled_id = await add_mailbox(sessionmaker, sync_enabled=False)
    requests = Requests()
    watcher = make_watcher(sessionmaker, migrated_database, requests, lambda config: fake)
    stop = asyncio.Event()
    task = asyncio.create_task(watcher.run(stop))
    try:
        # Initial sync on connect.
        await eventually(lambda: requests.count(mailbox_id) == 1)
        await eventually(lambda: bool(fake._watchers))
        fake.add_message("INBOX", b"Subject: x\r\n\r\nbody\r\n")
        await eventually(lambda: requests.count(mailbox_id) == 2)
        assert disabled_id not in watcher.watched

        await set_enabled(sessionmaker, mailbox_id, False)
        await eventually(lambda: mailbox_id not in watcher.watched)
        await eventually(lambda: not fake._watchers)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5)
    assert fake.closed


async def test_polls_when_push_is_unsupported(
    sessionmaker: async_sessionmaker[AsyncSession], migrated_database: str
) -> None:
    fake = FakeMailProvider(push=False)
    mailbox_id = await add_mailbox(sessionmaker)
    requests = Requests()
    watcher = make_watcher(
        sessionmaker, migrated_database, requests, lambda config: fake, poll_interval=0.05
    )
    stop = asyncio.Event()
    task = asyncio.create_task(watcher.run(stop))
    try:
        await eventually(lambda: requests.count(mailbox_id) >= 4)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5)


async def test_reconnects_with_backoff_and_survives_request_failures(
    sessionmaker: async_sessionmaker[AsyncSession], migrated_database: str
) -> None:
    attempts: list[str] = []

    def factory(config: MailboxConfig) -> FakeMailProvider:
        attempts.append("connect")
        if len(attempts) == 1:
            raise AuthenticationError()
        return FakeMailProvider(push=False)

    async def failing_request(mailbox_id: uuid.UUID) -> None:
        raise RuntimeError("queue unavailable")

    await add_mailbox(sessionmaker)
    watcher = make_watcher(
        sessionmaker,
        migrated_database,
        failing_request,
        factory,
        error_backoff=0.05,
        poll_interval=0.05,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(watcher.run(stop))
    try:
        await eventually(lambda: len(attempts) >= 2)
        await asyncio.sleep(0.2)
        assert not task.done()
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5)


async def test_each_mailbox_is_watched_by_one_process(
    sessionmaker: async_sessionmaker[AsyncSession], migrated_database: str
) -> None:
    mailbox_id = await add_mailbox(sessionmaker)
    requests = Requests()

    def factory(config: MailboxConfig) -> FakeMailProvider:
        return FakeMailProvider()

    first = make_watcher(sessionmaker, migrated_database, requests, factory)
    second = make_watcher(sessionmaker, migrated_database, requests, factory)
    stop_first, stop_second = asyncio.Event(), asyncio.Event()
    first_task = asyncio.create_task(first.run(stop_first))
    try:
        await eventually(lambda: mailbox_id in first.watched)
        second_task = asyncio.create_task(second.run(stop_second))
        try:
            await asyncio.sleep(0.4)
            assert mailbox_id not in second.watched
            # The first process stops: its locks are released, the second takes over.
            stop_first.set()
            await asyncio.wait_for(first_task, timeout=5)
            await eventually(lambda: mailbox_id in second.watched)
        finally:
            stop_second.set()
            await asyncio.wait_for(second_task, timeout=5)
    finally:
        stop_first.set()
        await asyncio.wait_for(first_task, timeout=5)
