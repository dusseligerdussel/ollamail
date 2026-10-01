"""Sync engine tests with ``FakeMailProvider`` (database needed, no mail server)."""

import io
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import LoggingSettings, MailSettings
from app.core.events import Event
from app.core.logging import configure_logging
from app.mail import hooks
from app.mail.models import Attachment, Folder, FolderRole, Mailbox, MailboxType, Message, SyncState
from app.mail.providers.base import (
    AuthenticationError,
    ConfigurationError,
    ConnectionFailedError,
    MailboxConfig,
    ProviderError,
    RemoteFolder,
    SyncCursor,
    SyncEvent,
)
from app.mail.providers.fake import FakeMailProvider
from app.mail.storage import AttachmentStorage
from app.mail.sync import engine
from app.mail.sync.engine import sync_mailbox
from tests.mail.conftest import load_fixture

pytestmark = pytest.mark.db

SETTINGS = MailSettings()
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def stored() -> Iterator[list[hooks.MessageStored]]:
    calls: list[hooks.MessageStored] = []

    async def record(event: hooks.MessageStored) -> None:
        calls.append(event)

    hooks.on_message_stored(record)
    yield calls
    hooks.remove_message_stored_handler(record)


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[tuple[uuid.UUID, Event]]:
    events: list[tuple[uuid.UUID, Event]] = []

    async def publish(session: AsyncSession, user_id: uuid.UUID, event: Event) -> None:
        events.append((user_id, event))

    monkeypatch.setattr(engine, "publish", publish)
    return events


@pytest.fixture
def fake() -> FakeMailProvider:
    provider = FakeMailProvider()
    provider.add_folder(RemoteFolder("INBOX", "Inbox", role=FolderRole.INBOX))
    provider.add_folder(RemoteFolder("Archive", "Archive", role=FolderRole.ARCHIVE))
    provider.add_folder(RemoteFolder("Trash", "Trash", role=FolderRole.TRASH))
    return provider


@pytest.fixture
async def mailbox(db_session: AsyncSession) -> Mailbox:
    mailbox = Mailbox(
        type=MailboxType.IMAP,
        display_name="Test",
        address="erika@example.org",
        owner_user_id=uuid.uuid4(),
    )
    db_session.add(mailbox)
    # Committed like a real mailbox: a rollback in the engine must not remove it.
    await db_session.commit()
    return mailbox


def mid(mailbox: Mailbox) -> uuid.UUID:
    """The mailbox ID without loading attributes (they expire on rollbacks in the engine)."""
    identity = inspect(mailbox).identity
    assert identity is not None
    value = identity[0]
    assert isinstance(value, uuid.UUID)
    return value


async def run(
    session: AsyncSession,
    mailbox: Mailbox,
    provider: Any,
    storage: AttachmentStorage,
    **kwargs: Any,
) -> engine.SyncStats | None:
    return await sync_mailbox(
        session,
        mid(mailbox),
        storage=storage,
        settings=SETTINGS,
        provider_factory=lambda config: provider,
        now=lambda: NOW,
        **kwargs,
    )


async def refs(session: AsyncSession, mailbox: Mailbox) -> set[str]:
    rows = await session.scalars(
        select(Message.remote_ref).where(Message.mailbox_id == mid(mailbox))
    )
    return set(rows)


async def state(session: AsyncSession, mailbox: Mailbox, remote_id: str | None) -> SyncState:
    query = select(SyncState).where(SyncState.mailbox_id == mid(mailbox))
    if remote_id is None:
        query = query.where(SyncState.folder_id.is_(None))
    else:
        query = query.join(Folder, Folder.id == SyncState.folder_id).where(
            Folder.remote_id == remote_id
        )
    result = await session.scalar(query.execution_options(populate_existing=True))
    assert result is not None
    return result


async def test_initial_sync_stores_new_messages_and_notifies_once(
    db_session: AsyncSession,
    mailbox: Mailbox,
    fake: FakeMailProvider,
    storage: AttachmentStorage,
    stored: list[hooks.MessageStored],
    published: list[tuple[uuid.UUID, Event]],
) -> None:
    inbox_ref = fake.add_message("INBOX", load_fixture("01-plain-ascii.eml"), received_at=NOW)
    archive_ref = fake.add_message(
        "Archive", load_fixture("05-nested-multipart.eml"), received_at=NOW
    )
    fake.add_message("Trash", load_fixture("09-signature-delimiter.eml"), received_at=NOW)
    # Older than the initial import window (90 days).
    old = NOW - timedelta(days=91)
    fake.add_message("INBOX", load_fixture("03-gmail-reply-utf8.eml"), received_at=old)

    stats = await run(db_session, mailbox, fake, storage)

    assert stats == engine.SyncStats(folders=2, fetched=2, stored=2)
    assert await refs(db_session, mailbox) == {inbox_ref, archive_ref}
    folders = {
        f.remote_id: f
        for f in await db_session.scalars(select(Folder).where(Folder.mailbox_id == mailbox.id))
    }
    assert set(folders) == {"INBOX", "Archive", "Trash"}
    # Trash is excluded by default (data minimisation), but the folder is known.
    assert not folders["Trash"].sync_enabled
    assert (await state(db_session, mailbox, "INBOX")).cursor["seq"] == 4
    mailbox_state = await state(db_session, mailbox, None)
    assert mailbox_state.last_synced_at == NOW and mailbox_state.last_error is None

    message_ids = set(await db_session.scalars(select(Message.id)))
    assert {call.message_id for call in stored} == message_ids
    assert len(stored) == 2 and all(call.mailbox_id == mailbox.id for call in stored)
    assert [event.status for _, event in published] == ["progress", "progress", "done"]
    assert {user for user, _ in published} == {mailbox.owner_user_id}

    # A second run fetches nothing new and does not notify again.
    stored.clear()
    stats = await run(db_session, mailbox, fake, storage)
    assert stats == engine.SyncStats(folders=2)
    assert stored == []


async def test_incremental_changes(
    db_session: AsyncSession,
    mailbox: Mailbox,
    fake: FakeMailProvider,
    storage: AttachmentStorage,
    stored: list[hooks.MessageStored],
) -> None:
    keep = fake.add_message("INBOX", load_fixture("01-plain-ascii.eml"))
    gone = fake.add_message("INBOX", load_fixture("05-nested-multipart.eml"))
    await run(db_session, mailbox, fake, storage)
    paths = list(await db_session.scalars(select(Attachment.storage_path)))
    assert paths and all(storage.exists(p) for p in paths)
    stored.clear()

    await fake.set_flags(keep, frozenset({"seen", "Wichtig"}))
    fake.delete_message(gone)
    new = fake.add_message("INBOX", load_fixture("09-signature-delimiter.eml"))
    stats = await run(db_session, mailbox, fake, storage)

    assert stats is not None
    assert (stats.stored, stats.updated, stats.deleted) == (1, 1, 1)
    assert await refs(db_session, mailbox) == {keep, new}
    flags = await db_session.scalar(select(Message.flags).where(Message.remote_ref == keep))
    assert flags == ["Wichtig", "seen"]
    assert not any(storage.exists(p) for p in paths)
    assert await db_session.scalar(select(func.count()).select_from(Attachment)) == 0
    assert [call.message_id for call in stored] == list(
        await db_session.scalars(select(Message.id).where(Message.remote_ref == new))
    )


async def test_invalid_cursor_reimports_the_folder(
    db_session: AsyncSession,
    mailbox: Mailbox,
    fake: FakeMailProvider,
    storage: AttachmentStorage,
    stored: list[hooks.MessageStored],
) -> None:
    ref = fake.add_message("INBOX", load_fixture("01-plain-ascii.eml"))
    await run(db_session, mailbox, fake, storage)
    first_id = await db_session.scalar(select(Message.id))

    fake.invalidate_cursors()
    stats = await run(db_session, mailbox, fake, storage)

    assert stats is not None and stats.deleted == 1 and stats.stored == 1
    assert await refs(db_session, mailbox) == {ref}
    assert await db_session.scalar(select(Message.id)) != first_id
    assert len(stored) == 2


async def test_folders_removed_on_the_server_are_deleted(
    db_session: AsyncSession,
    mailbox: Mailbox,
    fake: FakeMailProvider,
    storage: AttachmentStorage,
) -> None:
    fake.add_message("INBOX", load_fixture("01-plain-ascii.eml"))
    fake.add_message("Archive", load_fixture("09-signature-delimiter.eml"))
    await run(db_session, mailbox, fake, storage)

    del fake._state.folders["Archive"]
    stats = await run(db_session, mailbox, fake, storage)

    assert stats is not None and stats.deleted == 1
    remote_ids = set(
        await db_session.scalars(select(Folder.remote_id).where(Folder.mailbox_id == mailbox.id))
    )
    assert remote_ids == {"INBOX", "Trash"}
    assert await db_session.scalar(select(func.count()).select_from(Message)) == 1


async def test_excluded_folders_from_settings(
    db_session: AsyncSession, fake: FakeMailProvider, storage: AttachmentStorage
) -> None:
    mailbox = Mailbox(
        type=MailboxType.IMAP,
        display_name="Test",
        address="erika@example.org",
        owner_user_id=uuid.uuid4(),
        sync_settings={"excluded_roles": [], "excluded_folders": ["Archive"]},
    )
    db_session.add(mailbox)
    await db_session.commit()
    fake.add_message("Archive", load_fixture("01-plain-ascii.eml"))
    trash = fake.add_message("Trash", load_fixture("09-signature-delimiter.eml"))

    stats = await run(db_session, mailbox, fake, storage)

    assert stats is not None and stats.folders == 2
    assert await refs(db_session, mailbox) == {trash}


class FailingProvider:
    """Wraps the fake and fails at a chosen point."""

    def __init__(
        self,
        fake: FakeMailProvider,
        *,
        on_list: ProviderError | None = None,
        on_fetch: dict[str, Exception] | None = None,
        after_events: int | None = None,
    ) -> None:
        self.fake = fake
        self.capabilities = fake.capabilities
        self.on_list = on_list
        self.on_fetch = on_fetch or {}
        self.after_events = after_events
        self.closed = False

    async def list_folders(self) -> list[RemoteFolder]:
        if self.on_list is not None:
            raise self.on_list
        return await self.fake.list_folders()

    async def fetch_since(
        self, folder_id: str, cursor: SyncCursor | None, *, since: datetime | None = None
    ) -> AsyncIterator[SyncEvent]:
        if folder_id in self.on_fetch:
            raise self.on_fetch[folder_id]
        count = 0
        async for event in self.fake.fetch_since(folder_id, cursor, since=since):
            if self.after_events is not None and count == self.after_events:
                raise ConnectionFailedError()
            count += 1
            yield event

    async def aclose(self) -> None:
        self.closed = True


async def test_authentication_error_is_recorded_not_retried(
    db_session: AsyncSession,
    mailbox: Mailbox,
    fake: FakeMailProvider,
    storage: AttachmentStorage,
    published: list[tuple[uuid.UUID, Event]],
) -> None:
    provider = FailingProvider(fake, on_list=AuthenticationError())

    assert await run(db_session, mailbox, provider, storage) is None

    assert provider.closed
    assert (await state(db_session, mailbox, None)).last_error == "authentication_failed"
    assert [event.status for _, event in published] == ["failed"]


async def test_connection_errors_are_recorded_and_raised_for_retry(
    db_session: AsyncSession,
    mailbox: Mailbox,
    fake: FakeMailProvider,
    storage: AttachmentStorage,
    stored: list[hooks.MessageStored],
) -> None:
    for name in ("01-plain-ascii.eml", "05-nested-multipart.eml", "09-signature-delimiter.eml"):
        fake.add_message("INBOX", load_fixture(name))

    with pytest.raises(ConnectionFailedError):
        await run(db_session, mailbox, FailingProvider(fake, after_events=2), storage)

    # Nothing of the interrupted batch was committed or announced.
    assert await refs(db_session, mailbox) == set()
    assert stored == []
    assert (await state(db_session, mailbox, None)).last_error == "connection_failed"

    # The next run imports everything; every message is announced exactly once.
    await run(db_session, mailbox, fake, storage)
    assert len(await refs(db_session, mailbox)) == 3
    assert len(stored) == 3
    assert (await state(db_session, mailbox, None)).last_error is None


async def test_folder_errors_do_not_stop_other_folders(
    db_session: AsyncSession,
    mailbox: Mailbox,
    fake: FakeMailProvider,
    storage: AttachmentStorage,
) -> None:
    ref = fake.add_message("Archive", load_fixture("01-plain-ascii.eml"))
    provider = FailingProvider(fake, on_fetch={"INBOX": ProviderError(code="server_error")})

    stats = await run(db_session, mailbox, provider, storage)

    assert stats is not None and stats.failed_folders == 1
    assert await refs(db_session, mailbox) == {ref}
    assert (await state(db_session, mailbox, "INBOX")).last_error == "server_error"
    assert (await state(db_session, mailbox, None)).last_error is None


async def test_provider_configuration_errors_are_recorded(
    db_session: AsyncSession, mailbox: Mailbox, storage: AttachmentStorage
) -> None:
    def factory(config: MailboxConfig) -> Any:
        raise ConfigurationError(code="insecure_connection_refused")

    result = await sync_mailbox(
        db_session, mid(mailbox), storage=storage, settings=SETTINGS, provider_factory=factory
    )

    assert result is None
    assert (await state(db_session, mailbox, None)).last_error == "insecure_connection_refused"


async def test_missing_or_disabled_mailboxes_are_skipped(
    db_session: AsyncSession, mailbox: Mailbox, fake: FakeMailProvider, storage: AttachmentStorage
) -> None:
    mailbox.sync_enabled = False
    await db_session.commit()
    assert await run(db_session, mailbox, fake, storage) is None
    assert fake.actions == []
    result = await sync_mailbox(
        db_session,
        uuid.uuid4(),
        storage=storage,
        settings=SETTINGS,
        provider_factory=lambda config: fake,
    )
    assert result is None


async def test_failing_hook_does_not_stop_the_sync(
    db_session: AsyncSession, mailbox: Mailbox, fake: FakeMailProvider, storage: AttachmentStorage
) -> None:
    async def broken(event: hooks.MessageStored) -> None:
        raise RuntimeError("boom")

    hooks.on_message_stored(broken)
    try:
        fake.add_message("INBOX", load_fixture("01-plain-ascii.eml"))
        stats = await run(db_session, mailbox, fake, storage)
    finally:
        hooks.remove_message_stored_handler(broken)
    assert stats is not None and stats.stored == 1


async def test_no_mail_content_in_logs(
    db_session: AsyncSession, mailbox: Mailbox, fake: FakeMailProvider, storage: AttachmentStorage
) -> None:
    stream = io.StringIO()
    configure_logging(LoggingSettings(level="DEBUG"), stream=stream)
    fake.add_message("INBOX", load_fixture("02-outlook-reply-cp1252.eml"))
    await run(db_session, mailbox, fake, storage)
    await run(db_session, mailbox, FailingProvider(fake, on_list=AuthenticationError()), storage)

    output = stream.getvalue()
    assert "mail_sync_finished" in output and "mail_sync_failed" in output
    for secret in ("Nordlicht", "erika@example.org", "Kostenübersicht", "Mustermann", "Inbox"):
        assert secret not in output
