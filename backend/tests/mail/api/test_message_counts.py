"""Message counts of the sync status (#188): reused briefly while a mailbox syncs."""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.mail.api import service
from app.mail.models import Mailbox, MailboxType, Message
from tests.factories import make_user

pytestmark = pytest.mark.db


async def _mailbox(session: AsyncSession) -> Mailbox:
    user = await make_user(session)
    mailbox = Mailbox(
        type=MailboxType.IMAP,
        display_name="Work",
        address=f"{uuid.uuid4().hex[:8]}@example.org",
        owner_user_id=user.id,
    )
    session.add(mailbox)
    await session.flush()
    return mailbox


async def _add_messages(session: AsyncSession, mailbox: Mailbox, count: int) -> None:
    session.add_all(
        Message(mailbox_id=mailbox.id, remote_ref=uuid.uuid4().hex, subject="Test")
        for _ in range(count)
    )
    await session.flush()


async def test_busy_mailboxes_reuse_the_count_for_a_while(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    mailbox = await _mailbox(db_session)
    await _add_messages(db_session, mailbox, 2)
    assert await service.message_counts(db_session, {mailbox.id: "importing"}) == {mailbox.id: 2}

    await _add_messages(db_session, mailbox, 3)
    assert await service.message_counts(db_session, {mailbox.id: "importing"}) == {mailbox.id: 2}

    monkeypatch.setattr(service, "MESSAGE_COUNT_TTL_SECONDS", 0.0)
    assert await service.message_counts(db_session, {mailbox.id: "importing"}) == {mailbox.id: 5}


async def test_idle_mailboxes_are_counted_every_time(db_session: AsyncSession) -> None:
    mailbox = await _mailbox(db_session)
    empty = await _mailbox(db_session)
    await _add_messages(db_session, mailbox, 1)
    assert await service.message_counts(db_session, {mailbox.id: "syncing"}) == {mailbox.id: 1}

    await _add_messages(db_session, mailbox, 1)

    counts = await service.message_counts(db_session, {mailbox.id: "idle", empty.id: "idle"})
    assert counts == {mailbox.id: 2, empty.id: 0}
    # Once idle, an earlier busy count is not reused when the next sync starts.
    await _add_messages(db_session, mailbox, 1)
    assert await service.message_counts(db_session, {mailbox.id: "syncing"}) == {mailbox.id: 3}
