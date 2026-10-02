"""Sync engine with a mailbox-wide change log (``capabilities.mailbox_cursor``, e.g. Gmail),
using ``FakeMailProvider(mailbox_cursor=True)``. Database needed, no mail server."""

import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.mail import hooks
from app.mail.models import Folder, FolderRole, Mailbox, Message, SyncState
from app.mail.providers.base import (
    ConnectionFailedError,
    CursorAdvanced,
    MessageFetched,
    RemoteFolder,
    SyncCursor,
    SyncEvent,
)
from app.mail.providers.fake import FakeMailProvider
from app.mail.storage import AttachmentStorage
from app.mail.sync import engine
from tests.mail import test_sync
from tests.mail.conftest import load_fixture
from tests.mail.test_sync import NOW, mid, refs, run, state

pytestmark = pytest.mark.db

# Fixtures shared with the folder-based engine tests.
mailbox = test_sync.mailbox
stored = test_sync.stored
published = test_sync.published


@pytest.fixture
def fake() -> FakeMailProvider:
    provider = FakeMailProvider(labels=True, mailbox_cursor=True)
    provider.add_folder(RemoteFolder("INBOX", "Inbox", role=FolderRole.INBOX))
    provider.add_folder(RemoteFolder("TRASH", "Trash", role=FolderRole.TRASH))
    provider.add_folder(RemoteFolder("ALL", "All Mail", role=FolderRole.ALL))
    provider.add_folder(RemoteFolder("Label_1", "Receipts"))
    return provider


async def folder_states(session: AsyncSession, mailbox: Mailbox) -> int:
    rows = await session.scalars(
        select(SyncState).where(
            SyncState.mailbox_id == mid(mailbox), SyncState.folder_id.is_not(None)
        )
    )
    return len(list(rows))


async def message_folders(session: AsyncSession, mailbox: Mailbox, ref: str) -> set[str]:
    rows = await session.scalars(
        select(Folder.remote_id)
        .join(Message.folders)
        .where(Message.mailbox_id == mid(mailbox), Message.remote_ref == ref)
        .execution_options(populate_existing=True)
    )
    return set(rows)


async def test_one_fetch_for_the_whole_mailbox(
    db_session: AsyncSession,
    mailbox: Mailbox,
    fake: FakeMailProvider,
    storage: AttachmentStorage,
    stored: list[hooks.MessageStored],
) -> None:
    both = fake.add_message(
        ("INBOX", "Label_1", "ALL"), load_fixture("01-plain-ascii.eml"), received_at=NOW
    )
    archived = fake.add_message(("ALL",), load_fixture("05-nested-multipart.eml"), received_at=NOW)
    fake.add_message(("TRASH",), load_fixture("09-signature-delimiter.eml"), received_at=NOW)

    stats = await run(db_session, mailbox, fake, storage)

    assert stats == engine.SyncStats(folders=3, fetched=2, stored=2)
    # Trash is excluded by default: messages only there are not stored.
    assert await refs(db_session, mailbox) == {both, archived}
    assert await message_folders(db_session, mailbox, both) == {"INBOX", "Label_1", "ALL"}
    assert (await state(db_session, mailbox, None)).cursor == {"epoch": 0, "seq": 3}
    assert await folder_states(db_session, mailbox) == 0
    assert len(stored) == 2 and all(call.backfill for call in stored)


async def test_incremental_changes_and_leaving_selected_folders(
    db_session: AsyncSession,
    mailbox: Mailbox,
    fake: FakeMailProvider,
    storage: AttachmentStorage,
    stored: list[hooks.MessageStored],
) -> None:
    keep = fake.add_message(("INBOX", "ALL"), load_fixture("01-plain-ascii.eml"))
    trashed = fake.add_message(("INBOX", "ALL"), load_fixture("05-nested-multipart.eml"))
    gone = fake.add_message(("ALL",), load_fixture("09-signature-delimiter.eml"))
    await run(db_session, mailbox, fake, storage)
    stored.clear()

    await fake.apply_label(keep, "Label_1")
    await fake.set_flags(keep, frozenset({"seen"}))
    # Moved to trash: no selected folder left.
    await fake.remove_label(trashed, "INBOX")
    await fake.remove_label(trashed, "ALL")
    await fake.apply_label(trashed, "TRASH")
    fake.delete_message(gone)
    new = fake.add_message(("INBOX", "ALL"), load_fixture("03-gmail-reply-utf8.eml"))
    stats = await run(db_session, mailbox, fake, storage)

    assert stats is not None and (stats.stored, stats.deleted) == (1, 2)
    assert await refs(db_session, mailbox) == {keep, new}
    assert await message_folders(db_session, mailbox, keep) == {"INBOX", "ALL", "Label_1"}
    flags = await db_session.scalar(
        select(Message.flags)
        .where(Message.remote_ref == keep)
        .execution_options(populate_existing=True)
    )
    assert flags == ["seen"]
    assert [call.backfill for call in stored] == [False]


async def test_resync_keeps_known_messages(
    db_session: AsyncSession,
    mailbox: Mailbox,
    fake: FakeMailProvider,
    storage: AttachmentStorage,
    stored: list[hooks.MessageStored],
) -> None:
    keep = fake.add_message(("INBOX", "ALL"), load_fixture("01-plain-ascii.eml"), received_at=NOW)
    gone = fake.add_message(("ALL",), load_fixture("05-nested-multipart.eml"), received_at=NOW)
    old = NOW - timedelta(days=200)
    older = fake.add_message(("ALL",), load_fixture("09-signature-delimiter.eml"), received_at=NOW)
    await run(db_session, mailbox, fake, storage)
    # Imported with an earlier, larger window; now older than the window.
    await db_session.execute(
        Message.__table__.update().where(Message.remote_ref == older).values(received_at=old)
    )
    await db_session.commit()
    ids = {
        ref: id_ for ref, id_ in await db_session.execute(select(Message.remote_ref, Message.id))
    }
    stored.clear()

    fake.invalidate_cursors()
    fake.delete_message(gone)
    fake.delete_message(older)
    new = fake.add_message(
        ("INBOX", "ALL"), load_fixture("03-gmail-reply-utf8.eml"), received_at=NOW
    )
    stats = await run(db_session, mailbox, fake, storage)

    assert stats is not None and (stats.stored, stats.deleted) == (1, 1)
    # The known message keeps its row (and with it triage, todos, ...).
    current = {
        ref: id_ for ref, id_ in await db_session.execute(select(Message.remote_ref, Message.id))
    }
    assert current[keep] == ids[keep]
    # Outside the window nothing is verified: the old message stays.
    assert set(current) == {keep, older, new}
    assert [call.backfill for call in stored] == [True]
    assert (await state(db_session, mailbox, None)).cursor == {"epoch": 1, "seq": 6}


class _Interrupting:
    """Wraps a provider and breaks the connection after the first fetched message."""

    def __init__(self, provider: FakeMailProvider) -> None:
        self.provider = provider
        self.capabilities = provider.capabilities

    def __getattr__(self, name: str) -> Any:
        return getattr(self.provider, name)

    async def fetch_since(
        self, folder_id: str, cursor: SyncCursor | None, *, since: datetime | None = None
    ) -> AsyncIterator[SyncEvent]:
        async for event in self.provider.fetch_since(folder_id, cursor, since=since):
            yield event
            if isinstance(event, MessageFetched):
                yield CursorAdvanced(SyncCursor({"epoch": 99, "seq": 0}))
                raise ConnectionFailedError()


async def test_interrupted_resync_starts_over(
    db_session: AsyncSession,
    mailbox: Mailbox,
    fake: FakeMailProvider,
    storage: AttachmentStorage,
) -> None:
    first = fake.add_message(("ALL",), load_fixture("01-plain-ascii.eml"), received_at=NOW)
    gone = fake.add_message(("ALL",), load_fixture("05-nested-multipart.eml"), received_at=NOW)
    await run(db_session, mailbox, fake, storage)
    fake.invalidate_cursors()
    fake.delete_message(gone)

    with pytest.raises(ConnectionFailedError):
        await run(db_session, mailbox, _Interrupting(fake), storage)

    # No intermediate cursor: the next run resyncs (and reconciles) again.
    assert (await state(db_session, mailbox, None)).cursor == {}
    assert await refs(db_session, mailbox) == {first, gone}
    await run(db_session, mailbox, fake, storage)
    assert await refs(db_session, mailbox) == {first}


async def test_excluding_a_folder_skips_its_messages(
    db_session: AsyncSession,
    mailbox: Mailbox,
    fake: FakeMailProvider,
    storage: AttachmentStorage,
) -> None:
    mailbox.sync_settings = {"excluded_folders": ["ALL", "Label_1"]}
    await db_session.commit()
    inbox = fake.add_message(("INBOX", "ALL"), load_fixture("01-plain-ascii.eml"), received_at=NOW)
    fake.add_message(("Label_1", "ALL"), load_fixture("05-nested-multipart.eml"), received_at=NOW)

    await run(db_session, mailbox, fake, storage)

    assert await refs(db_session, mailbox) == {inbox}


async def test_provider_errors_fail_the_mailbox(
    db_session: AsyncSession,
    mailbox: Mailbox,
    fake: FakeMailProvider,
    storage: AttachmentStorage,
    published: list[tuple[uuid.UUID, Any]],
) -> None:
    from app.mail.providers.base import ProviderError

    class Broken(FakeMailProvider):
        async def fetch_since(  # type: ignore[override]
            self, folder_id: str, cursor: SyncCursor | None, *, since: datetime | None = None
        ) -> AsyncIterator[SyncEvent]:
            raise ProviderError(code="server_error")
            yield  # pragma: no cover

    broken = Broken(labels=True, mailbox_cursor=True)
    broken.add_folder(RemoteFolder("INBOX", "Inbox", role=FolderRole.INBOX))

    assert await run(db_session, mailbox, broken, storage) is None
    assert (await state(db_session, mailbox, None)).last_error == "server_error"
    assert [event.status for _, event in published] == ["failed"]
