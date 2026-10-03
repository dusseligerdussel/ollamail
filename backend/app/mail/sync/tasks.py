"""Worker tasks of the mail sync (queue ``sync``).

``mail.sync_mailbox`` syncs one mailbox. Jobs for the same mailbox never run in parallel
(``lock``) and at most one waits in the queue (``queueing_lock``), so ``request_sync`` can
be called as often as changes are noticed (push, polling, API).

A large import runs in time slices (#141): a run that stops at the end of its slice
(``SyncStats.incomplete``) queues a follow-up run right away, at a lower priority
(``CONTINUE_PRIORITY``), so syncs of other mailboxes go first. The follow-up fetches new
mail before it continues the import.

``mail.write_flags`` writes read/unread changes made in the app back to the server.
"""

import uuid
from functools import cache

from procrastinate.exceptions import AlreadyEnqueued

from app.core.config import get_settings
from app.core.db import Database
from app.mail.flags import write_flags
from app.mail.storage import AttachmentStorage
from app.mail.sync.engine import sync_mailbox
from app.worker import DEFAULT_RETRY, app, resource_lock

# Priority of the follow-up run of a sliced import; requested syncs have priority 0.
CONTINUE_PRIORITY = -5


@cache
def _database() -> Database:
    # One engine per worker process, created on first use inside the worker's event loop.
    return Database(get_settings().database)


@app.task(name="mail.sync_mailbox", queue="sync", retry=DEFAULT_RETRY)
async def sync_mailbox_job(mailbox_id: str) -> None:
    settings = get_settings()
    async with _database().sessionmaker() as session:
        stats = await sync_mailbox(
            session,
            uuid.UUID(mailbox_id),
            storage=AttachmentStorage(settings.storage.data_dir),
            settings=settings.mail,
        )
    if stats is not None and stats.incomplete:
        # A sync that is already waiting continues the import just as well.
        await request_sync(uuid.UUID(mailbox_id), priority=CONTINUE_PRIORITY)


async def request_sync(mailbox_id: uuid.UUID, *, priority: int = 0) -> bool:
    """Queue a sync of the mailbox; ``False`` if one is already waiting."""
    lock = resource_lock("mailbox", mailbox_id)
    try:
        await sync_mailbox_job.configure(
            lock=lock, queueing_lock=lock, priority=priority
        ).defer_async(mailbox_id=str(mailbox_id))
    except AlreadyEnqueued:
        return False
    return True


@app.task(name="mail.write_flags", queue="sync", retry=DEFAULT_RETRY)
async def write_flags_job(message_id: str) -> None:
    async with _database().sessionmaker() as session:
        await write_flags(session, uuid.UUID(message_id))


async def request_flag_write(message_id: uuid.UUID) -> None:
    """Queue writing the stored flags of a message to the server. Jobs of one message run
    one after another and each writes the state at the time it runs."""
    lock = resource_lock("message_flags", message_id)
    await write_flags_job.configure(lock=lock).defer_async(message_id=str(message_id))
