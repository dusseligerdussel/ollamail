"""Message lists newest first (``app.mail.listing``) and the ``sort_date`` column (#140)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from app.mail import listing
from app.mail.models import UNREAD_PREDICATE, FolderRole, Message
from app.mail.providers.base import Flag
from tests.triage.conftest import account_for, make_account

DAY = datetime(2026, 9, 1, tzinfo=UTC)


@pytest.mark.db
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


@pytest.mark.db
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
    assert await listing.count(db_session, folders) == 6

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
    assert await listing.count(db_session, []) == 0


def test_unread_filter_is_the_predicate_of_the_partial_index() -> None:
    """The planner uses ``ix_mail_messages_unread`` only for exactly its predicate, with the
    array as a literal (#186)."""
    compiled = str(listing.UNREAD.compile(dialect=postgresql.dialect()))
    assert compiled == f"NOT (mail_messages.{UNREAD_PREDICATE.removeprefix('NOT (')}"
    assert f"ARRAY['{Flag.SEEN.value}']" in UNREAD_PREDICATE
    assert listing.read_state(None) == []


@pytest.mark.db
async def test_counts_and_snippets(db_session: AsyncSession) -> None:
    account = await make_account(db_session)
    unread = await account.message("Unread", body="word " * 100)
    seen = await account.message("Seen")
    seen.flags = [Flag.SEEN.value]
    await account.message("Archived", in_inbox=False)
    await db_session.flush()
    folders = await listing.folder_ids(db_session, [account.mailbox.id], FolderRole.INBOX)
    assert await listing.folder_ids(db_session, [account.mailbox.id], account.inbox.id) == folders
    other = await make_account(db_session)
    assert await listing.folder_ids(db_session, [account.mailbox.id], other.inbox.id) == []

    assert await listing.count(db_session, folders) == 2
    assert await listing.count(db_session, folders, listing.read_state(True)) == 1
    assert await listing.count(db_session, folders, listing.read_state(False)) == 1

    db_session.expunge_all()
    rows = list(
        await db_session.scalars(
            listing.newest_first(
                [account.mailbox.id],
                [listing.in_folders(folders), *listing.read_state(True)],
                before=None,
                limit=10,
            )
        )
    )
    assert [m.id for m in rows] == [unread.id]
    # Only the start of the body is read; the body itself stays unloaded.
    assert rows[0].snippet == ("word " * 100)[: listing.SNIPPET_LENGTH]
    assert "body_main" not in rows[0].__dict__
