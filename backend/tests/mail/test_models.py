"""Database tests for the mail model: cascades, constraints, storing and threading."""

import io
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import LoggingSettings
from app.core.logging import configure_logging
from app.mail.credentials import CredentialsEncryptionUnavailableError
from app.mail.models import (
    Attachment,
    Folder,
    FolderRole,
    Mailbox,
    MailboxType,
    Message,
    SyncState,
    Thread,
    message_folders,
)
from app.mail.providers.base import RawMessage
from app.mail.service import delete_mailbox, delete_messages, store_message
from app.mail.storage import AttachmentStorage
from tests.mail.conftest import load_fixture

pytestmark = pytest.mark.db


async def make_mailbox(session: AsyncSession, **kwargs: object) -> Mailbox:
    values: dict[str, object] = {
        "type": MailboxType.IMAP,
        "display_name": "Test",
        "address": "erika@example.org",
        "owner_user_id": uuid.uuid4(),
    }
    values.update(kwargs)
    mailbox = Mailbox(**values)
    session.add(mailbox)
    await session.flush()
    return mailbox


async def make_folders(session: AsyncSession, mailbox: Mailbox) -> dict[str, Folder]:
    folders = {
        "INBOX": Folder(
            mailbox_id=mailbox.id, remote_id="INBOX", name="Inbox", role=FolderRole.INBOX
        ),
        "Archive": Folder(mailbox_id=mailbox.id, remote_id="Archive", name="Archive"),
    }
    session.add_all(folders.values())
    await session.flush()
    return folders


def raw(
    name: str, *, provider_thread_id: str | None = None, flags: frozenset[str] = frozenset()
) -> RawMessage:
    return RawMessage(
        remote_ref=name,
        raw=load_fixture(name),
        folder_ids=("INBOX",),
        flags=flags,
        provider_thread_id=provider_thread_id,
    )


async def count(session: AsyncSession, model: type[object]) -> int:
    return await session.scalar(select(func.count()).select_from(model)) or 0


async def test_mailbox_owner_is_user_or_shared(db_session: AsyncSession) -> None:
    await make_mailbox(db_session)
    await make_mailbox(db_session, owner_user_id=None, is_shared=True)

    for invalid in ({"owner_user_id": None}, {"is_shared": True}):
        async with db_session.begin_nested() as nested:
            with pytest.raises(IntegrityError):
                await make_mailbox(db_session, **invalid)
            await nested.rollback()


async def test_mailbox_type_is_checked(db_session: AsyncSession) -> None:
    with pytest.raises(StatementError):
        await make_mailbox(db_session, type="pop3")


async def test_credentials_cannot_be_stored_unencrypted(db_session: AsyncSession) -> None:
    with pytest.raises(StatementError) as info:
        await make_mailbox(db_session, credentials={"password": "secret"})

    assert isinstance(info.value.orig, CredentialsEncryptionUnavailableError)


async def test_store_message_persists_normalised_data(
    db_session: AsyncSession, storage: AttachmentStorage
) -> None:
    mailbox = await make_mailbox(db_session)
    folders = await make_folders(db_session, mailbox)

    message = await store_message(
        db_session,
        mailbox.id,
        raw("05-nested-multipart.eml", flags=frozenset({"seen"})),
        storage,
        folders,
    )
    await db_session.commit()
    await db_session.refresh(message, ["attachments", "folders"])

    assert message.subject == "Quarterly report Q3"
    assert message.sender == {"name": "Jordan Example", "address": "jordan@example.com"}
    assert message.language == "en"
    assert message.flags == ["seen"]
    assert message.has_attachments
    assert [f.remote_id for f in message.folders] == ["INBOX"]
    logo, report = message.attachments
    assert logo.is_inline and logo.content_id == "logo-0001@example.com"
    assert storage.read(report.storage_path).startswith(b"%PDF")
    # File names on disk never contain the original filename.
    assert "report" not in report.storage_path


async def test_store_message_is_idempotent_and_updates_flags(
    db_session: AsyncSession, storage: AttachmentStorage
) -> None:
    mailbox = await make_mailbox(db_session)
    folders = await make_folders(db_session, mailbox)
    first = await store_message(db_session, mailbox.id, raw("01-plain-ascii.eml"), storage, folders)

    again = await store_message(
        db_session,
        mailbox.id,
        RawMessage(
            remote_ref="01-plain-ascii.eml",
            raw=b"",
            folder_ids=("Archive",),
            flags=frozenset({"seen", "flagged"}),
        ),
        storage,
        folders,
    )

    assert again.id == first.id
    assert again.flags == ["flagged", "seen"]
    assert [f.remote_id for f in again.folders] == ["Archive"]
    assert await count(db_session, Message) == 1


async def test_threading_by_references_and_subject(
    db_session: AsyncSession, storage: AttachmentStorage
) -> None:
    mailbox = await make_mailbox(db_session)
    folders = await make_folders(db_session, mailbox)

    async def store(name: str) -> Message:
        return await store_message(db_session, mailbox.id, raw(name), storage, folders)

    original = await store("01-plain-ascii.eml")  # "Meeting tomorrow"
    reply = await store("10-mobile-signature.eml")  # In-Reply-To the original
    gmail = await store("03-gmail-reply-utf8.eml")  # parent not stored
    chained = await store("13-reply-chain-references.eml")  # references the gmail reply
    unrelated = await store("09-signature-delimiter.eml")

    assert reply.thread_id == original.thread_id
    assert chained.thread_id == gmail.thread_id
    assert len({original.thread_id, gmail.thread_id, unrelated.thread_id}) == 3

    # Subject fallback: a reply without References joins the thread with the same subject.
    no_refs = (
        load_fixture("01-plain-ascii.eml")
        .replace(b"Subject: Meeting tomorrow", b"Subject: RE: AW: Meeting tomorrow")
        .replace(b"Message-ID: <plain-0001@example.com>", b"Message-ID: <norefs@example.com>")
    )
    fallback = await store_message(
        db_session, mailbox.id, RawMessage("norefs", no_refs, ("INBOX",)), storage, folders
    )
    assert fallback.thread_id == original.thread_id

    thread = await db_session.get(Thread, original.thread_id)
    assert thread is not None
    assert thread.subject_key == "meeting tomorrow"
    assert thread.last_message_at == datetime(2026, 9, 1, 7, 40, tzinfo=UTC)


async def test_child_stored_before_parent_shares_thread(
    db_session: AsyncSession, storage: AttachmentStorage
) -> None:
    mailbox = await make_mailbox(db_session)
    folders = await make_folders(db_session, mailbox)

    reply = await store_message(
        db_session, mailbox.id, raw("10-mobile-signature.eml"), storage, folders
    )
    original = await store_message(
        db_session, mailbox.id, raw("01-plain-ascii.eml"), storage, folders
    )

    assert original.thread_id == reply.thread_id


async def test_provider_thread_id_wins(
    db_session: AsyncSession, storage: AttachmentStorage
) -> None:
    mailbox = await make_mailbox(db_session, type=MailboxType.GMAIL)
    folders = await make_folders(db_session, mailbox)

    first = await store_message(
        db_session,
        mailbox.id,
        raw("01-plain-ascii.eml", provider_thread_id="t-1"),
        storage,
        folders,
    )
    second = await store_message(
        db_session,
        mailbox.id,
        raw("09-signature-delimiter.eml", provider_thread_id="t-1"),
        storage,
        folders,
    )

    assert first.thread_id == second.thread_id


async def test_threads_never_span_mailboxes(
    db_session: AsyncSession, storage: AttachmentStorage
) -> None:
    a = await make_mailbox(db_session)
    b = await make_mailbox(db_session)
    folders_a = await make_folders(db_session, a)
    folders_b = await make_folders(db_session, b)

    original = await store_message(db_session, a.id, raw("01-plain-ascii.eml"), storage, folders_a)
    reply = await store_message(
        db_session, b.id, raw("10-mobile-signature.eml"), storage, folders_b
    )

    assert original.thread_id != reply.thread_id


async def test_sync_state_is_unique_per_folder_and_mailbox(db_session: AsyncSession) -> None:
    mailbox = await make_mailbox(db_session)
    folders = await make_folders(db_session, mailbox)
    db_session.add(SyncState(mailbox_id=mailbox.id, folder_id=folders["INBOX"].id))
    db_session.add(SyncState(mailbox_id=mailbox.id, folder_id=None, cursor={"history_id": "1"}))
    await db_session.flush()

    for folder_id in (folders["INBOX"].id, None):
        async with db_session.begin_nested() as nested:
            db_session.add(SyncState(mailbox_id=mailbox.id, folder_id=folder_id))
            with pytest.raises(IntegrityError):
                await db_session.flush()
            await nested.rollback()


async def test_delete_messages_removes_rows_and_files(
    db_session: AsyncSession, storage: AttachmentStorage
) -> None:
    mailbox = await make_mailbox(db_session)
    folders = await make_folders(db_session, mailbox)
    message = await store_message(
        db_session, mailbox.id, raw("05-nested-multipart.eml"), storage, folders
    )
    paths = [a.storage_path for a in message.attachments]
    await db_session.commit()

    deleted = await delete_messages(db_session, mailbox.id, ["05-nested-multipart.eml"], storage)

    assert deleted == 1
    assert await count(db_session, Message) == 0
    assert await count(db_session, Attachment) == 0
    assert not any(storage.exists(p) for p in paths)
    assert await delete_messages(db_session, mailbox.id, [], storage) == 0


async def test_delete_mailbox_cascades_everything(
    db_session: AsyncSession, storage: AttachmentStorage
) -> None:
    mailbox = await make_mailbox(db_session)
    other = await make_mailbox(db_session)
    folders = await make_folders(db_session, mailbox)
    other_folders = await make_folders(db_session, other)
    for name in ("05-nested-multipart.eml", "12-broken-headers-rfc2231.eml", "01-plain-ascii.eml"):
        await store_message(db_session, mailbox.id, raw(name), storage, folders)
    kept = await store_message(
        db_session, other.id, raw("05-nested-multipart.eml"), storage, other_folders
    )
    db_session.add(SyncState(mailbox_id=mailbox.id, folder_id=folders["INBOX"].id))
    await db_session.commit()
    assert storage.mailbox_dir(mailbox.id).exists()

    assert await delete_mailbox(db_session, mailbox.id, storage)

    db_session.expunge_all()
    for model in (Folder, Thread, Message, Attachment, SyncState):
        remaining = await db_session.scalar(
            select(func.count()).select_from(model).where(model.mailbox_id == mailbox.id)
        )
        assert remaining == 0, model.__name__
    links = await db_session.scalar(select(func.count()).select_from(message_folders))
    assert links == 1  # only the other mailbox's message
    assert not storage.mailbox_dir(mailbox.id).exists()
    # Other mailboxes are untouched.
    assert await db_session.get(Message, kept.id) is not None
    assert all(storage.exists(a.storage_path) for a in kept.attachments)
    assert not await delete_mailbox(db_session, mailbox.id, storage)


async def test_no_mail_content_in_logs(
    db_session: AsyncSession, storage: AttachmentStorage
) -> None:
    stream = io.StringIO()
    configure_logging(LoggingSettings(level="DEBUG"), stream=stream)
    mailbox = await make_mailbox(db_session)
    folders = await make_folders(db_session, mailbox)

    await store_message(
        db_session, mailbox.id, raw("02-outlook-reply-cp1252.eml"), storage, folders
    )
    await delete_mailbox(db_session, mailbox.id, storage)

    output = stream.getvalue()
    assert "mail_message_stored" in output
    for secret in ("Nordlicht", "erika@example.org", "Kostenübersicht", "Mustermann"):
        assert secret not in output
