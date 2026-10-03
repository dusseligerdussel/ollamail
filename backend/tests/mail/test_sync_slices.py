"""Time slices of a large import (#141): a sync run stops importing after
``OLLAMAIL_MAIL_SYNC_SLICE_BATCHES`` batches or ``..._SLICE_MINUTES`` minutes, and the next
run fetches new mail before it continues. Synthetic data, database needed."""

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import MailSettings
from app.mail import hooks
from app.mail.models import FolderRole, Mailbox, MailboxType, Message
from app.mail.providers.base import (
    CursorAdvanced,
    MessageFetched,
    ProviderCapabilities,
    RawMessage,
    RemoteFolder,
    SyncCursor,
    SyncEvent,
)
from app.mail.storage import AttachmentStorage
from app.mail.sync import tasks
from app.mail.sync.engine import SyncStats, sync_mailbox
from tests.factories import make_user
from tests.mail.conftest import load_fixture

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
BATCH = 2


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class PagedProvider:
    """Like the real providers: per folder, new mail first (``high``), then the import
    from newest to oldest in batches, with the position in the cursor."""

    capabilities = ProviderCapabilities()

    def __init__(self, clock: Clock, batch_minutes: float = 0) -> None:
        self.new: dict[str, list[str]] = {"INBOX": [], "Archive": []}
        self.old: dict[str, list[str]] = {"INBOX": [], "Archive": []}
        self.clock = clock
        self.batch_minutes = batch_minutes
        # Import batches handed out, per folder.
        self.batches: dict[str, int] = {"INBOX": 0, "Archive": 0}

    async def list_folders(self) -> list[RemoteFolder]:
        return [
            RemoteFolder("INBOX", "Inbox", role=FolderRole.INBOX),
            RemoteFolder("Archive", "Archive", role=FolderRole.ARCHIVE),
        ]

    def _raw(self, ref: str, folder: str) -> RawMessage:
        return RawMessage(ref, load_fixture("01-plain-ascii.eml"), (folder,), received_at=NOW)

    async def fetch_since(
        self, folder_id: str, cursor: SyncCursor | None, *, since: datetime | None = None
    ) -> AsyncIterator[SyncEvent]:
        data = dict(cursor.data) if cursor else {"high": len(self.new[folder_id]), "pos": 0}
        new = self.new[folder_id][int(data["high"]) :]
        if new:
            for ref in new:
                yield MessageFetched(self._raw(ref, folder_id))
            data["high"] = len(self.new[folder_id])
            yield CursorAdvanced(SyncCursor(dict(data)))
        old = self.old[folder_id]
        while int(data["pos"]) < len(old):
            pos = int(data["pos"])
            self.batches[folder_id] += 1
            self.clock.now += timedelta(minutes=self.batch_minutes)
            for ref in old[pos : pos + BATCH]:
                yield MessageFetched(self._raw(ref, folder_id), initial=True)
            data["pos"] = pos + BATCH
            yield CursorAdvanced(SyncCursor(dict(data)))
        yield CursorAdvanced(SyncCursor(dict(data)))

    async def aclose(self) -> None:
        return None


@pytest.fixture
def stored() -> Iterator[list[hooks.MessageStored]]:
    calls: list[hooks.MessageStored] = []

    async def record(event: hooks.MessageStored) -> None:
        calls.append(event)

    hooks.on_message_stored(record)
    yield calls
    hooks.remove_message_stored_handler(record)


@pytest.fixture
async def mailbox_id(db_session: AsyncSession) -> uuid.UUID:
    mailbox = Mailbox(
        type=MailboxType.IMAP,
        display_name="Test",
        address="erika@example.org",
        owner_user_id=(await make_user(db_session)).id,
    )
    db_session.add(mailbox)
    await db_session.commit()
    return mailbox.id


async def _run(
    session: AsyncSession,
    mailbox_id: uuid.UUID,
    provider: PagedProvider,
    storage: AttachmentStorage,
    **settings: Any,
) -> SyncStats:
    stats = await sync_mailbox(
        session,
        mailbox_id,
        storage=storage,
        settings=MailSettings(**settings),
        provider_factory=lambda config: provider,
        now=provider.clock,
    )
    assert stats is not None
    return stats


async def _refs(session: AsyncSession, mailbox_id: uuid.UUID) -> set[str]:
    rows = await session.scalars(select(Message.remote_ref).where(Message.mailbox_id == mailbox_id))
    return set(rows)


def _old(count: int, folder: str = "INBOX") -> list[str]:
    return [f"{folder}-old-{n}" for n in range(count)]


@pytest.mark.db
async def test_import_stops_after_the_batch_limit_and_resumes(
    db_session: AsyncSession, mailbox_id: uuid.UUID, storage: AttachmentStorage
) -> None:
    provider = PagedProvider(Clock())
    provider.old["INBOX"] = _old(9)

    first = await _run(db_session, mailbox_id, provider, storage, sync_slice_batches=2)
    assert first.incomplete and first.stored == 4 and first.import_batches == 2
    # The next batch was not even requested from the server.
    assert provider.batches["INBOX"] == 2
    assert await _refs(db_session, mailbox_id) == set(_old(4))

    second = await _run(db_session, mailbox_id, provider, storage, sync_slice_batches=2)
    assert second.incomplete and second.stored == 4

    third = await _run(db_session, mailbox_id, provider, storage, sync_slice_batches=2)
    assert not third.incomplete and third.stored == 1
    assert await _refs(db_session, mailbox_id) == set(_old(9))
    assert provider.batches["INBOX"] == 5


@pytest.mark.db
async def test_import_stops_after_the_time_limit(
    db_session: AsyncSession, mailbox_id: uuid.UUID, storage: AttachmentStorage
) -> None:
    # Every batch takes 2 minutes; the slice is 5 minutes long.
    provider = PagedProvider(Clock(), batch_minutes=2)
    provider.old["INBOX"] = _old(10)

    stats = await _run(
        db_session, mailbox_id, provider, storage, sync_slice_batches=0, sync_slice_minutes=5
    )

    assert stats.incomplete and stats.stored == 6
    assert provider.batches["INBOX"] == 3


@pytest.mark.db
async def test_no_limit_imports_everything_in_one_run(
    db_session: AsyncSession, mailbox_id: uuid.UUID, storage: AttachmentStorage
) -> None:
    provider = PagedProvider(Clock(), batch_minutes=60)
    provider.old["INBOX"] = _old(10)

    stats = await _run(
        db_session, mailbox_id, provider, storage, sync_slice_batches=0, sync_slice_minutes=0
    )

    assert not stats.incomplete and stats.stored == 10


@pytest.mark.db
async def test_new_mail_is_fetched_between_slices(
    db_session: AsyncSession,
    mailbox_id: uuid.UUID,
    storage: AttachmentStorage,
    stored: list[hooks.MessageStored],
) -> None:
    provider = PagedProvider(Clock())
    provider.old["INBOX"] = _old(6)
    provider.old["Archive"] = _old(4, "Archive")
    await _run(db_session, mailbox_id, provider, storage, sync_slice_batches=1)
    # The slice ended in the INBOX: the Archive import did not start.
    assert await _refs(db_session, mailbox_id) == set(_old(2))

    stored.clear()
    provider.new["INBOX"] = ["fresh"]
    stats = await _run(db_session, mailbox_id, provider, storage, sync_slice_batches=1)

    assert stats.incomplete
    # The new mail first, as new mail (processing priority), then one import batch.
    first = stored[0]
    assert not first.backfill
    ref = await db_session.scalar(select(Message.remote_ref).where(Message.id == first.message_id))
    assert ref == "fresh"
    assert [call.backfill for call in stored] == [False, True, True]


@pytest.mark.db
async def test_new_mail_in_later_folders_is_not_cut_off(
    db_session: AsyncSession, mailbox_id: uuid.UUID, storage: AttachmentStorage
) -> None:
    provider = PagedProvider(Clock())
    provider.old["Archive"] = ["archived"]
    await _run(db_session, mailbox_id, provider, storage)

    provider.new["Archive"] = ["filed"]
    # A late INBOX import (e.g. a larger import window) that uses up the slice at once.
    provider.old["INBOX"] = _old(4)

    stats = await _run(db_session, mailbox_id, provider, storage, sync_slice_batches=1)

    assert stats.incomplete
    assert "filed" in await _refs(db_session, mailbox_id)


async def test_incomplete_run_queues_a_follow_up(monkeypatch: pytest.MonkeyPatch) -> None:
    requested: list[tuple[uuid.UUID, int]] = []
    results = iter([SyncStats(incomplete=True), SyncStats(), None])

    async def fake_sync(session: Any, mailbox_id: uuid.UUID, **kwargs: Any) -> SyncStats | None:
        return next(results)

    async def fake_request(mailbox_id: uuid.UUID, *, priority: int = 0) -> bool:
        requested.append((mailbox_id, priority))
        return True

    class FakeDatabase:
        def sessionmaker(self) -> Any:
            class Session:
                async def __aenter__(self) -> None:
                    return None

                async def __aexit__(self, *args: object) -> None:
                    return None

            return Session()

    monkeypatch.setattr(tasks, "sync_mailbox", fake_sync)
    monkeypatch.setattr(tasks, "request_sync", fake_request)
    monkeypatch.setattr(tasks, "_database", lambda: FakeDatabase())
    mailbox_id = uuid.uuid4()

    for _ in range(3):
        await tasks.sync_mailbox_job(str(mailbox_id))

    # Only after the incomplete run, behind requested syncs.
    assert requested == [(mailbox_id, tasks.CONTINUE_PRIORITY)]
    assert tasks.CONTINUE_PRIORITY < 0
