"""Message counts of the sync status: reused briefly while a mailbox syncs (#188) and
while an idle mailbox has not synced again (#226)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.mail.api import service
from app.mail.api.service import CountStamp
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


@pytest.fixture(autouse=True)
def _empty_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "_message_counts", {})


SYNCED = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


async def test_busy_mailboxes_reuse_the_count_for_a_while(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    mailbox = await _mailbox(db_session)
    importing = {mailbox.id: CountStamp("importing")}
    await _add_messages(db_session, mailbox, 2)
    assert await service.message_counts(db_session, importing) == {mailbox.id: 2}

    await _add_messages(db_session, mailbox, 3)
    assert await service.message_counts(db_session, importing) == {mailbox.id: 2}

    monkeypatch.setattr(service, "MESSAGE_COUNT_TTL_SECONDS", 0.0)
    assert await service.message_counts(db_session, importing) == {mailbox.id: 5}


async def test_idle_mailboxes_reuse_the_count_until_a_sync_ends(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#226: an idle mailbox is not counted again on every request."""
    mailbox = await _mailbox(db_session)
    empty = await _mailbox(db_session)
    await _add_messages(db_session, mailbox, 1)
    idle = CountStamp("idle", SYNCED)
    assert await service.message_counts(db_session, {mailbox.id: CountStamp("syncing")}) == {
        mailbox.id: 1
    }

    await _add_messages(db_session, mailbox, 1)
    # The phase changed since the busy count: counted again.
    counts = await service.message_counts(db_session, {mailbox.id: idle, empty.id: idle})
    assert counts == {mailbox.id: 2, empty.id: 0}

    # Unchanged sync state: reused, even after the busy TTL.
    monkeypatch.setattr(service, "MESSAGE_COUNT_TTL_SECONDS", 0.0)
    await _add_messages(db_session, mailbox, 1)
    assert await service.message_counts(db_session, {mailbox.id: idle}) == {mailbox.id: 2}

    # A sync ended (between two requests): counted again.
    later = CountStamp("idle", SYNCED + timedelta(minutes=5))
    assert await service.message_counts(db_session, {mailbox.id: later}) == {mailbox.id: 3}

    # Deletions without a sync (retention): counted again after the stable TTL.
    await _add_messages(db_session, mailbox, 1)
    monkeypatch.setattr(service, "STABLE_MESSAGE_COUNT_TTL_SECONDS", 0.0)
    assert await service.message_counts(db_session, {mailbox.id: later}) == {mailbox.id: 4}


async def test_idle_count_is_not_reused_when_the_next_sync_starts(
    db_session: AsyncSession,
) -> None:
    mailbox = await _mailbox(db_session)
    await _add_messages(db_session, mailbox, 1)
    idle = {mailbox.id: CountStamp("idle", SYNCED)}
    assert await service.message_counts(db_session, idle) == {mailbox.id: 1}

    await _add_messages(db_session, mailbox, 1)
    syncing = {mailbox.id: CountStamp("syncing", SYNCED)}
    assert await service.message_counts(db_session, syncing) == {mailbox.id: 2}
    # And a busy count is not reused once the mailbox is idle again.
    await _add_messages(db_session, mailbox, 1)
    assert await service.message_counts(db_session, idle) == {mailbox.id: 3}
