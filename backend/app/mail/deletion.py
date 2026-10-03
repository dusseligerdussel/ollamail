"""Removing a mailbox in the background (#147).

A mailbox can hold 200k mails with attachments, search chunks and embeddings. Deleting it
with one cascading statement inside the HTTP request kept the request (and locks on all
those rows) open for minutes. Instead:

1. ``request_deletion`` (in the request) marks the mailbox (``deletion_requested_at``),
   pauses its sync and records ``mailbox.deleted`` in the audit log. From the commit on,
   ``app.mail.access`` hides the mailbox and everything derived from it from every mail
   query (inbox, search, todos, digest, ...); only the mailbox list shows it, with the
   status ``deleting``.
2. The job ``mail.delete_mailbox`` deletes the mails in batches (newest first, one commit
   per batch, attachment files after each commit, like ``app.privacy.retention``), then
   the threads, then the mailbox row with what is left (folders, sync state, settings)
   and the attachment directory. Readers get ``mailbox.changed`` with ``deleted``, the
   handlers of ``app.mail.hooks.on_mailbox_deleted`` the former owner (a user being
   deleted waits for their mailboxes, ``app.privacy.deletion``).

The job is idempotent: a retry, or the periodic ``mail.resume_deletions`` after a lost
job, continues where the previous run stopped.
"""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache

from procrastinate.exceptions import AlreadyEnqueued
from sqlalchemy import delete, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.core.config import get_settings
from app.core.db import Database
from app.core.events import Event, publish
from app.core.logging import get_logger
from app.mail import hooks
from app.mail.access import publish_to_readers, reader_ids
from app.mail.models import Attachment, Mailbox, Message, Thread
from app.mail.storage import AttachmentStorage
from app.worker import DEFAULT_RETRY, app, resource_lock

log = get_logger(__name__)

# Mails per transaction; each cascades to attachments, search chunks, embeddings,
# processing and triage rows, so a batch stays well below a second.
MESSAGE_BATCH = 500
THREAD_BATCH = 2000

DELETE_TASK = "mail.delete_mailbox"


def _mailbox_changed(mailbox_id: uuid.UUID, status: str) -> Event:
    return Event(type="mailbox.changed", ids={"mailbox_id": mailbox_id}, status=status)


async def request_deletion(session: AsyncSession, mailbox: Mailbox, actor: audit.Actor) -> None:
    """Mark ``mailbox`` for removal, pause its sync, record ``mailbox.deleted`` and tell
    its readers (``mailbox.changed`` ``deleting``). The caller commits, then queues the
    job with ``defer_deletion``."""
    if mailbox.deletion_requested_at is None:
        mailbox.deletion_requested_at = datetime.now(UTC)
        mailbox.sync_enabled = False
        target = audit.Target.of(audit.TargetType.MAILBOX, mailbox.id)
        await audit.record(session, actor, audit.AuditAction.MAILBOX_DELETED, target)
    await publish_to_readers(session, mailbox, _mailbox_changed(mailbox.id, "deleting"))


@dataclass
class PurgeResult:
    messages: int = 0
    threads: int = 0
    # The mailbox row was deleted by this run.
    deleted: bool = False
    owner_user_id: uuid.UUID | None = None


async def _delete_files(storage: AttachmentStorage, paths: list[str]) -> None:
    for path in paths:
        try:
            await asyncio.to_thread(storage.delete, path)
        except ValueError:
            # A stored path outside the attachment directory is never touched.
            log.warning("mail_mailbox_deletion_invalid_path")


async def purge_mailbox(
    session: AsyncSession, mailbox_id: uuid.UUID, storage: AttachmentStorage
) -> PurgeResult:
    """Delete a mailbox marked by ``request_deletion`` with all its data, in batches.
    Commits per batch. Does nothing for a mailbox that is not marked (or gone)."""
    result = PurgeResult()
    marked = (
        await session.execute(
            select(Mailbox.id, Mailbox.owner_user_id).where(
                Mailbox.id == mailbox_id, Mailbox.deletion_requested_at.is_not(None)
            )
        )
    ).first()
    if marked is None:
        # Already gone (a previous run finished): remove leftover files only.
        if await session.get(Mailbox, mailbox_id) is None:
            await asyncio.to_thread(storage.delete_mailbox, mailbox_id)
        return result
    # Keyset over the list index (mailbox, sort_date, id): each batch starts below the
    # previous one instead of skipping the index entries of the rows deleted so far.
    after: tuple[datetime, uuid.UUID] | None = None
    while True:
        query = select(Message.id, Message.sort_date).where(Message.mailbox_id == mailbox_id)
        if after is not None:
            query = query.where(tuple_(Message.sort_date, Message.id) < after)
        rows = (
            await session.execute(
                query.order_by(Message.sort_date.desc(), Message.id.desc()).limit(MESSAGE_BATCH)
            )
        ).all()
        if not rows:
            break
        ids = [row.id for row in rows]
        after = (rows[-1].sort_date, rows[-1].id)
        paths = list(
            await session.scalars(
                select(Attachment.storage_path).where(Attachment.message_id.in_(ids))
            )
        )
        await session.execute(delete(Message).where(Message.id.in_(ids)))
        await session.commit()
        await _delete_files(storage, paths)
        result.messages += len(ids)
    while True:
        ids = list(
            await session.scalars(
                select(Thread.id).where(Thread.mailbox_id == mailbox_id).limit(THREAD_BATCH)
            )
        )
        if not ids:
            break
        await session.execute(delete(Thread).where(Thread.id.in_(ids)))
        await session.commit()
        result.threads += len(ids)
    # Readers are found through the mailbox row, so before it goes.
    readers = await reader_ids(session, mailbox_id)
    deleted = await session.execute(
        delete(Mailbox).where(Mailbox.id == mailbox_id).returning(Mailbox.id)
    )
    result.deleted = deleted.first() is not None
    result.owner_user_id = marked.owner_user_id if result.deleted else None
    for user_id in readers:
        await publish(session, user_id, _mailbox_changed(mailbox_id, "deleted"))
    await session.commit()
    await asyncio.to_thread(storage.delete_mailbox, mailbox_id)
    log.info(
        "mail_mailbox_deleted",
        mailbox_id=str(mailbox_id),
        message_count=result.messages,
        thread_count=result.threads,
    )
    return result


@cache
def _database() -> Database:
    # One engine per worker process, created on first use inside the worker's event loop.
    return Database(get_settings().database)


@app.task(name=DELETE_TASK, queue="default", retry=DEFAULT_RETRY)
async def delete_mailbox_job(mailbox_id: str) -> None:
    async with _database().sessionmaker() as session:
        result = await purge_mailbox(
            session, uuid.UUID(mailbox_id), AttachmentStorage(get_settings().storage.data_dir)
        )
    if result.deleted:
        await hooks.mailbox_deleted(uuid.UUID(mailbox_id), result.owner_user_id)


async def defer_deletion(mailbox_id: uuid.UUID) -> bool:
    """Queue the removal of a marked mailbox; ``False`` if it is already waiting.

    Its own lock, not the sync's: a long initial import must not hold up the removal. A
    sync that is still running stops at its next write (the mailbox is gone)."""
    lock = resource_lock("mailbox_deletion", mailbox_id)
    try:
        await delete_mailbox_job.configure(lock=lock, queueing_lock=lock).defer_async(
            mailbox_id=str(mailbox_id)
        )
    except AlreadyEnqueued:
        return False
    return True


async def pending_deletions(session: AsyncSession) -> list[uuid.UUID]:
    return list(
        await session.scalars(
            select(Mailbox.id)
            .where(Mailbox.deletion_requested_at.is_not(None))
            .order_by(Mailbox.deletion_requested_at)
        )
    )


@app.periodic(cron="*/15 * * * *", periodic_id="mail.resume_deletions")
@app.task(
    name="mail.resume_deletions",
    queue="default",
    queueing_lock="mail.resume_deletions",
    lock="mail.resume_deletions",
    retry=DEFAULT_RETRY,
)
async def resume_deletions(timestamp: int) -> None:
    """Queue removals whose job got lost (queueing failed after the request committed, or
    the job failed for good). Waiting or running ones are left alone (``queueing_lock``)."""
    async with _database().sessionmaker() as session:
        mailbox_ids = await pending_deletions(session)
    queued = 0
    for mailbox_id in mailbox_ids:
        queued += await defer_deletion(mailbox_id)
    if queued:
        log.info("mail_mailbox_deletions_resumed", count=queued)
