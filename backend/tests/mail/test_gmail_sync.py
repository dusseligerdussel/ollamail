"""Gmail provider driven by the real sync engine against the synthetic Gmail API
(respx + PostgreSQL): initial import, incremental history, expired history."""

from collections.abc import Iterator

import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import GmailSettings, MailSettings
from app.core.crypto import KeyRing, decode_key, generate_key, set_keyring
from app.mail import hooks
from app.mail.models import Folder, Mailbox, MailboxType, Message
from app.mail.providers.base import MailboxConfig
from app.mail.providers.gmail import ALL_MAIL, GmailProvider
from app.mail.storage import AttachmentStorage
from app.mail.sync import engine
from app.mail.sync.engine import sync_mailbox
from tests.factories import make_user
from tests.mail import test_sync
from tests.mail.gmail_server import GmailServer, StaticTokens
from tests.mail.test_gmail_provider import RECENT, mail, no_sleep
from tests.mail.test_sync import NOW, mid, refs, state

pytestmark = pytest.mark.db

stored = test_sync.stored


@pytest.fixture(autouse=True)
def keyring() -> Iterator[None]:
    # Credentials are encrypted (EncryptedJSON).
    set_keyring(KeyRing(decode_key(generate_key())))
    yield
    set_keyring(None)


@pytest.fixture
def server() -> Iterator[GmailServer]:
    gmail = GmailServer()
    with respx.mock(assert_all_called=False) as router:
        gmail.install(router)
        yield gmail


@pytest.fixture
async def mailbox(db_session: AsyncSession, server: GmailServer) -> Mailbox:
    mailbox = Mailbox(
        type=MailboxType.GMAIL,
        display_name="Test",
        address=server.address,
        owner_user_id=(await make_user(db_session)).id,
        provider_settings={"auth": "oauth"},
        credentials={"refresh_token": "test-refresh-token"},
    )
    db_session.add(mailbox)
    await db_session.commit()
    return mailbox


def factory(config: MailboxConfig) -> GmailProvider:
    return GmailProvider(
        config,
        settings=GmailSettings(),
        mail_settings=MailSettings(sync_batch_size=2),
        token_source=StaticTokens(),
        sleep=no_sleep,
    )


async def run(
    session: AsyncSession, mailbox: Mailbox, storage: AttachmentStorage
) -> engine.SyncStats | None:
    return await sync_mailbox(
        session,
        mid(mailbox),
        storage=storage,
        settings=MailSettings(),
        provider_factory=factory,
        now=lambda: NOW,
    )


async def test_gmail_end_to_end(
    db_session: AsyncSession,
    mailbox: Mailbox,
    server: GmailServer,
    storage: AttachmentStorage,
    stored: list[hooks.MessageStored],
) -> None:
    receipts = server.add_label("Receipts")
    inbox = server.add_message(mail(1), ["INBOX", "UNREAD"], internal_date=RECENT)
    labelled = server.add_message(mail(2), [receipts, "STARRED"], internal_date=RECENT + 1)
    server.add_message(mail(3), ["TRASH"], internal_date=RECENT + 2)
    sent = server.add_message(mail(4), ["SENT"], internal_date=RECENT + 3)
    history_id = server.history_id

    stats = await run(db_session, mailbox, storage)

    assert stats is not None and (stats.stored, stats.folders) == (3, 5)
    assert await refs(db_session, mailbox) == {inbox, labelled, sent}
    folders = {
        f.remote_id: f.sync_enabled
        for f in await db_session.scalars(select(Folder).where(Folder.mailbox_id == mid(mailbox)))
    }
    assert folders == {
        "INBOX": True,
        "SENT": True,
        "DRAFT": True,
        "SPAM": False,
        "TRASH": False,
        ALL_MAIL: True,
        receipts: True,
    }
    assert (await state(db_session, mailbox, None)).cursor == {
        "v": 1,
        "history_id": str(history_id),
    }
    message = await db_session.scalar(select(Message).where(Message.remote_ref == labelled))
    assert message is not None
    assert message.subject == "Synthetic message 2"
    assert message.flags == ["flagged", "seen"]

    # Incremental: new mail, archive, trash.
    stored.clear()
    new = server.add_message(mail(5), ["INBOX", "UNREAD"], internal_date=RECENT + 10)
    server.change_labels(inbox, remove=["INBOX"])
    server.change_labels(sent, add=["TRASH"])
    stats = await run(db_session, mailbox, storage)

    assert stats is not None and (stats.stored, stats.deleted) == (1, 1)
    assert await refs(db_session, mailbox) == {inbox, labelled, new}
    assert [call.backfill for call in stored] == [False]
    archived = await db_session.scalars(
        select(Folder.remote_id)
        .join(Message.folders)
        .where(Message.remote_ref == inbox)
        .execution_options(populate_existing=True)
    )
    assert set(archived) == {ALL_MAIL}
    assert (await state(db_session, mailbox, None)).cursor["history_id"] == str(server.history_id)

    # Expired history: resync without recreating known messages.
    ids = set(await db_session.scalars(select(Message.id)))
    stored.clear()
    server.delete_message(labelled)
    server.expire_history()
    stats = await run(db_session, mailbox, storage)

    assert stats is not None and (stats.stored, stats.deleted) == (0, 1)
    assert await refs(db_session, mailbox) == {inbox, new}
    assert set(await db_session.scalars(select(Message.id))) < ids
    assert stored == []
    mailbox_state = await state(db_session, mailbox, None)
    assert mailbox_state.cursor == {"v": 1, "history_id": str(server.history_id)}
    assert mailbox_state.last_error is None


async def test_revoked_token_is_recorded(
    db_session: AsyncSession, mailbox: Mailbox, server: GmailServer, storage: AttachmentStorage
) -> None:
    import httpx

    server.fail("GET", "/labels$", httpx.Response(401), times=2)

    assert await run(db_session, mailbox, storage) is None
    assert (await state(db_session, mailbox, None)).last_error == "authentication_failed"
