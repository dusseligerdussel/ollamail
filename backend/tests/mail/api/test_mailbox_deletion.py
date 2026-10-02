"""Removing a mailbox deletes every row and file derived from it (docs/PRIVACY.md, Art. 17).

The tables are discovered from the migrated schema, not listed by hand: every table that
references a mailbox, directly or through other tables (mails, threads, folders, ...), is
checked. Tables added later by other modules (triage, search index, ...) are covered
automatically; they only have to hang off the mailbox or the mail with ``ON DELETE
CASCADE``.
"""

import uuid
from collections import defaultdict

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditAction
from app.mail.models import Attachment, Mailbox, Message, Thread
from app.mail.storage import AttachmentStorage
from app.processing.models import MailboxProcessingSettings, MessageProcessing, StepStatus
from app.todos.models import Todo
from tests.audit.conftest import audit_rows
from tests.mail.api.conftest import FakeServer, add_mailbox, run_sync

pytestmark = pytest.mark.db

MAILBOXES = "mail_mailboxes"

# (child table, parent table, ON DELETE action) for every foreign key in the schema.
FOREIGN_KEYS = text(
    """
    SELECT child.relname::text, parent.relname::text, c.confdeltype::text
    FROM pg_constraint c
    JOIN pg_class child ON child.oid = c.conrelid
    JOIN pg_class parent ON parent.oid = c.confrelid
    JOIN pg_namespace n ON n.oid = child.relnamespace
    WHERE c.contype = 'f' AND n.nspname = current_schema()
    """
)
CASCADE, SET_NULL = "c", "n"


async def foreign_keys(session: AsyncSession) -> list[tuple[str, str, str]]:
    return [
        (child, parent, action) for child, parent, action in await session.execute(FOREIGN_KEYS)
    ]


def dependent_tables(keys: list[tuple[str, str, str]]) -> set[str]:
    """Tables that reference a mailbox directly or indirectly."""
    children: dict[str, set[str]] = defaultdict(set)
    for child, parent, _ in keys:
        children[parent].add(child)
    found: set[str] = set()
    pending = [MAILBOXES]
    while pending:
        for child in children[pending.pop()] - found:
            found.add(child)
            pending.append(child)
    found.discard(MAILBOXES)
    return found


def cascading_tables(keys: list[tuple[str, str, str]]) -> set[str]:
    """Tables whose rows are deleted (by cascade) when their mailbox is deleted."""
    found = {MAILBOXES}
    changed = True
    while changed:
        changed = False
        for child, parent, action in keys:
            if action == CASCADE and parent in found and child not in found:
                found.add(child)
                changed = True
    found.discard(MAILBOXES)
    return found


async def row_counts(session: AsyncSession, tables: set[str]) -> dict[str, int]:
    counts = {}
    for table in sorted(tables):
        counts[table] = await session.scalar(text(f'SELECT count(*) FROM "{table}"')) or 0
    return counts


async def test_every_table_referencing_a_mailbox_is_deleted_with_it(
    db_session: AsyncSession,
) -> None:
    keys = await foreign_keys(db_session)
    dependent = dependent_tables(keys)

    # Sanity check: the discovery sees the known modules.
    assert {
        "mail_folders",
        "mail_messages",
        "mail_attachments",
        "mail_message_folders",
        "mail_sync_states",
        "mail_threads",
        "message_processing",
        "processing_mailbox_settings",
        "todos",
    } <= dependent
    # Rows that would stay behind (or block the delete) after their mailbox is gone.
    assert dependent - cascading_tables(keys) == set()
    blocking = [
        (child, parent, action)
        for child, parent, action in keys
        if parent in dependent | {MAILBOXES}
        and action not in (CASCADE, SET_NULL)
        and child != MAILBOXES
    ]
    assert blocking == []


async def test_delete_removes_all_rows_and_files(
    erika: AsyncClient,
    bob: AsyncClient,
    server: FakeServer,
    storage: AttachmentStorage,
    db_session: AsyncSession,
) -> None:
    dependent = dependent_tables(await foreign_keys(db_session))
    # Another user's mailbox with data must survive.
    other_id = uuid.UUID(str((await add_mailbox(bob, address="bob@example.org"))["id"]))
    await run_sync(db_session, other_id, server, storage)
    baseline = await row_counts(db_session, dependent)

    mailbox_id = uuid.UUID(str((await add_mailbox(erika))["id"]))
    await run_sync(db_session, mailbox_id, server, storage)
    mailbox = await db_session.get(Mailbox, mailbox_id)
    assert mailbox is not None and mailbox.owner_user_id is not None
    message = await db_session.scalar(
        select(Message).where(Message.mailbox_id == mailbox_id, Message.has_attachments)
    )
    assert message is not None
    db_session.add_all(
        [
            MessageProcessing(
                message_id=message.id, step="todos", version=1, status=StepStatus.DONE
            ),
            MailboxProcessingSettings(mailbox_id=mailbox_id, enabled=False),
            Todo(
                user_id=mailbox.owner_user_id,
                mailbox_id=mailbox_id,
                message_id=message.id,
                thread_id=message.thread_id,
                title="Send the report",
            ),
        ]
    )
    await db_session.commit()

    filled = await row_counts(db_session, dependent)
    for table in (
        "mail_folders",
        "mail_messages",
        "mail_attachments",
        "mail_message_folders",
        "mail_sync_states",
        "mail_threads",
        "message_processing",
        "processing_mailbox_settings",
        "todos",
    ):
        assert filled[table] > baseline[table], table
    paths = list(
        await db_session.scalars(
            select(Attachment.storage_path).where(Attachment.mailbox_id == mailbox_id)
        )
    )
    assert paths and all(storage.exists(path) for path in paths)
    messages = await db_session.scalar(
        select(func.count()).select_from(Message).where(Message.mailbox_id == mailbox_id)
    )

    response = await erika.delete(f"/mailboxes/{mailbox_id}")

    assert response.status_code == 200
    assert response.json() == {
        "mailbox_id": str(mailbox_id),
        "deleted": True,
        "messages": messages,
        "attachments": len(paths),
    }
    db_session.expunge_all()
    assert await db_session.get(Mailbox, mailbox_id) is None
    [entry] = await audit_rows(db_session, AuditAction.MAILBOX_DELETED)
    assert (entry.actor_kind, entry.actor_id) == ("user", mailbox.owner_user_id)
    assert entry.target_id == str(mailbox_id)
    assert await row_counts(db_session, dependent) == baseline
    assert (
        await db_session.scalar(
            select(func.count()).select_from(Thread).where(Thread.mailbox_id == mailbox_id)
        )
        == 0
    )
    assert not any(storage.exists(path) for path in paths)
    assert not storage.mailbox_dir(mailbox_id).exists()
    # The other user's mailbox and files are untouched.
    assert storage.mailbox_dir(other_id).exists()
    assert (await bob.get(f"/mailboxes/{other_id}")).status_code == 200
    assert (await erika.get(f"/mailboxes/{mailbox_id}")).status_code == 404
    assert (await erika.delete(f"/mailboxes/{mailbox_id}")).status_code == 404


def test_discovery_flags_rows_that_would_stay_behind() -> None:
    keys = [
        ("mail_messages", MAILBOXES, CASCADE),
        ("summaries", "mail_messages", SET_NULL),
        ("labels", "summaries", CASCADE),
        ("users", "unrelated", CASCADE),
    ]

    assert dependent_tables(keys) == {"mail_messages", "summaries", "labels"}
    assert cascading_tables(keys) == {"mail_messages"}
