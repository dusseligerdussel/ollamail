"""Message lists newest first (``app.mail.listing``) and the ``sort_date`` column (#140)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.mail import listing
from app.mail.models import FolderRole, Message
from tests.triage.conftest import account_for, make_account

pytestmark = pytest.mark.db

DAY = datetime(2026, 9, 1, tzinfo=UTC)


async def test_sort_date_follows_received_sent_and_created(db_session: AsyncSession) -> None:
    account = await make_account(db_session)
    received = await account.message("Received")
    received.sent_at = DAY - timedelta(days=1)
    only_sent = await account.message("Sent")
    only_sent.received_at = None
    only_sent.sent_at = DAY
    neither = await account.message("Neither")
    neither.received_at = None
    await db_session.flush()

    # Read back from the trigger on insert and update (eager defaults), no lazy load.
    assert received.sort_date == received.received_at
    assert only_sent.sort_date == DAY
    assert neither.sort_date == neither.created_at


async def test_newest_first_merges_mailboxes_and_pages(db_session: AsyncSession) -> None:
    first = await make_account(db_session)
    second = await account_for(db_session, first.user)
    for index in range(6):
        account = first if index % 2 else second
        message = await account.message(f"Mail {index}")
        message.received_at = DAY + timedelta(hours=index)
    await account.message("Archived", in_inbox=False)
    other = await make_account(db_session)
    await other.message("Foreign")
    await db_session.flush()

    mailbox_ids = await listing.readable_mailbox_ids(db_session, first.user.id)
    folders = await listing.folder_ids(db_session, mailbox_ids, FolderRole.INBOX)
    conditions = [listing.in_folders(folders)]
    assert await listing.count(db_session, mailbox_ids, conditions) == 6

    subjects: list[str] = []
    before = None
    while True:
        page: list[Message] = list(
            await db_session.scalars(
                listing.newest_first(mailbox_ids, conditions, before=before, limit=4)
            )
        )
        subjects += [m.subject for m in page[:3]]
        if len(page) <= 3:
            break
        before = (page[2].sort_date, page[2].id)
    assert subjects == [f"Mail {index}" for index in reversed(range(6))]

    only_second = [second.mailbox.id]
    assert [
        m.subject
        for m in await db_session.scalars(
            listing.newest_first(only_second, conditions, before=None, limit=10)
        )
    ] == ["Mail 4", "Mail 2", "Mail 0"]
    assert list(await db_session.scalars(listing.newest_first([], [], before=None, limit=5))) == []
    assert await listing.count(db_session, [], conditions) == 0
