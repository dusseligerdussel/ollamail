"""Removing a mailbox in the background (#147): marking, batches, events, queueing.

The whole flow through the API (every dependent table empty, files gone) is covered by
``tests/mail/api/test_mailbox_deletion.py``; what readers still see while the job runs by
``tests/shared/test_access.py``.
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.audit import AuditAction
from app.core.events import Event
from app.mail import deletion, hooks
from app.mail.models import Mailbox, Message, Thread
from app.mail.service import store_message
from app.mail.storage import AttachmentStorage
from app.worker import app
from tests.audit.conftest import audit_rows
from tests.mail.test_models import make_folders, make_mailbox, raw
from tests.mail.test_sync_tasks import queue  # noqa: F401

pytestmark = pytest.mark.db

FIXTURES = ("05-nested-multipart.eml", "12-broken-headers-rfc2231.eml", "01-plain-ascii.eml")


async def filled_mailbox(session: AsyncSession, storage: AttachmentStorage) -> Mailbox:
    mailbox = await make_mailbox(session)
    folders = await make_folders(session, mailbox)
    for name in FIXTURES:
        await store_message(session, mailbox.id, raw(name), storage, folders)
    await session.commit()
    return mailbox


async def count(session: AsyncSession, model: Any, mailbox_id: uuid.UUID) -> int:
    return (
        await session.scalar(
            select(func.count()).select_from(model).where(model.mailbox_id == mailbox_id)
        )
        or 0
    )


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[tuple[uuid.UUID, Event]]:
    sent: list[tuple[uuid.UUID, Event]] = []

    async def publish(_: AsyncSession, user_id: uuid.UUID, event: Event) -> None:
        sent.append((user_id, event))

    monkeypatch.setattr(deletion, "publish", publish)
    return sent


async def test_request_marks_once_and_deletes_nothing(
    db_session: AsyncSession, storage: AttachmentStorage
) -> None:
    mailbox = await filled_mailbox(db_session, storage)
    assert mailbox.owner_user_id is not None
    actor = audit.Actor.user(mailbox.owner_user_id)

    await deletion.request_deletion(db_session, mailbox, actor)
    marked_at = mailbox.deletion_requested_at
    await deletion.request_deletion(db_session, mailbox, actor)
    await db_session.commit()

    assert marked_at is not None and mailbox.deletion_requested_at == marked_at
    assert mailbox.sync_enabled is False
    assert len(await audit_rows(db_session, AuditAction.MAILBOX_DELETED)) == 1
    # Nothing is deleted in the request.
    assert await count(db_session, Message, mailbox.id) == len(FIXTURES)


async def test_purge_deletes_in_batches_and_notifies_readers(
    db_session: AsyncSession,
    storage: AttachmentStorage,
    monkeypatch: pytest.MonkeyPatch,
    published: list[tuple[uuid.UUID, Event]],
) -> None:
    mailbox = await filled_mailbox(db_session, storage)
    other = await filled_mailbox(db_session, storage)
    await deletion.request_deletion(db_session, mailbox, audit.SYSTEM)
    await db_session.commit()
    commits = 0
    original_commit = db_session.commit

    async def commit() -> None:
        nonlocal commits
        commits += 1
        await original_commit()

    monkeypatch.setattr(db_session, "commit", commit)
    monkeypatch.setattr(deletion, "MESSAGE_BATCH", 2)

    result = await deletion.purge_mailbox(db_session, mailbox.id, storage)

    assert (result.messages, result.deleted) == (len(FIXTURES), True)
    # Two message batches, at least one thread batch, the mailbox.
    assert commits >= 4
    db_session.expunge_all()
    assert await db_session.get(Mailbox, mailbox.id) is None
    for model in (Message, Thread):
        assert await count(db_session, model, mailbox.id) == 0
    assert not storage.mailbox_dir(mailbox.id).exists()
    assert [(user_id, event.status) for user_id, event in published] == [
        (mailbox.owner_user_id, "deleted")
    ]
    # Other mailboxes are untouched.
    assert await count(db_session, Message, other.id) == len(FIXTURES)
    assert storage.mailbox_dir(other.id).exists()


async def test_purge_leaves_unmarked_mailboxes_alone(
    db_session: AsyncSession, storage: AttachmentStorage
) -> None:
    mailbox = await filled_mailbox(db_session, storage)

    assert await deletion.purge_mailbox(db_session, mailbox.id, storage) == deletion.PurgeResult()

    assert await count(db_session, Message, mailbox.id) == len(FIXTURES)
    assert storage.mailbox_dir(mailbox.id).exists()


async def test_purge_of_a_removed_mailbox_cleans_up_leftover_files(
    db_session: AsyncSession, storage: AttachmentStorage
) -> None:
    mailbox_id = uuid.uuid4()
    storage.write(mailbox_id, uuid.uuid4(), b"left behind by a crash")

    assert await deletion.purge_mailbox(db_session, mailbox_id, storage) == deletion.PurgeResult()

    assert not storage.mailbox_dir(mailbox_id).exists()


async def test_resume_queues_every_marked_mailbox(
    db_session: AsyncSession, storage: AttachmentStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    marked = await filled_mailbox(db_session, storage)
    await filled_mailbox(db_session, storage)
    await deletion.request_deletion(db_session, marked, audit.SYSTEM)
    await db_session.commit()
    deferred: list[uuid.UUID] = []

    async def defer(mailbox_id: uuid.UUID) -> bool:
        deferred.append(mailbox_id)
        return True

    class Database:
        @asynccontextmanager
        async def sessionmaker(self) -> AsyncIterator[AsyncSession]:
            yield db_session

    monkeypatch.setattr(deletion, "_database", Database)
    monkeypatch.setattr(deletion, "defer_deletion", defer)

    await deletion.resume_deletions(timestamp=0)

    assert deferred == [marked.id]


async def test_defer_queues_one_job_per_mailbox(queue: None) -> None:  # noqa: F811
    first, second = uuid.uuid4(), uuid.uuid4()

    assert await deletion.defer_deletion(first) is True
    assert await deletion.defer_deletion(first) is False  # one is already waiting
    assert await deletion.defer_deletion(second) is True

    jobs = await app.job_manager.list_jobs_async(task=deletion.DELETE_TASK)
    assert sorted(str(job.task_kwargs["mailbox_id"]) for job in jobs) == sorted(
        [str(first), str(second)]
    )
    assert {job.queue for job in jobs} == {"default"}
    # Not the sync's lock: a running import does not hold up the removal.
    assert {job.lock for job in jobs} == {f"mailbox_deletion:{first}", f"mailbox_deletion:{second}"}


async def test_job_reports_the_removed_mailbox_with_its_owner(
    db_session: AsyncSession, storage: AttachmentStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    mailbox = await filled_mailbox(db_session, storage)
    unmarked = await filled_mailbox(db_session, storage)
    await deletion.request_deletion(db_session, mailbox, audit.SYSTEM)
    await db_session.commit()
    reported: list[hooks.MailboxDeleted] = []

    async def handler(event: hooks.MailboxDeleted) -> None:
        reported.append(event)

    async def failing(event: hooks.MailboxDeleted) -> None:
        raise RuntimeError("handler failed")

    class Database:
        @asynccontextmanager
        async def sessionmaker(self) -> AsyncIterator[AsyncSession]:
            yield db_session

    monkeypatch.setattr(deletion, "_database", Database)
    monkeypatch.setattr(deletion, "AttachmentStorage", lambda _: storage)
    # A failing handler does not stop the others (the user deletion, #177).
    monkeypatch.setattr(hooks, "_deleted_handlers", [failing, handler])

    await deletion.delete_mailbox_job(str(mailbox.id))
    await deletion.delete_mailbox_job(str(mailbox.id))  # a retry reports nothing
    await deletion.delete_mailbox_job(str(unmarked.id))

    assert reported == [hooks.MailboxDeleted(mailbox.id, mailbox.owner_user_id)]
