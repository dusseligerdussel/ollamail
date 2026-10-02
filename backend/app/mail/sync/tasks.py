"""Worker tasks of the mail sync (queue ``sync``).

``mail.sync_mailbox`` syncs one mailbox. Jobs for the same mailbox never run in parallel
(``lock``) and at most one waits in the queue (``queueing_lock``), so ``request_sync`` can
be called as often as changes are noticed (push, polling, API).
"""

import uuid
from functools import cache

from procrastinate.exceptions import AlreadyEnqueued

from app.core.config import get_settings
from app.core.db import Database
from app.mail.storage import AttachmentStorage
from app.mail.sync.engine import sync_mailbox
from app.worker import DEFAULT_RETRY, app, resource_lock


@cache
def _database() -> Database:
    # One engine per worker process, created on first use inside the worker's event loop.
    return Database(get_settings().database)


@app.task(name="mail.sync_mailbox", queue="sync", retry=DEFAULT_RETRY)
async def sync_mailbox_job(mailbox_id: str) -> None:
    settings = get_settings()
    async with _database().sessionmaker() as session:
        await sync_mailbox(
            session,
            uuid.UUID(mailbox_id),
            storage=AttachmentStorage(settings.storage.data_dir),
            settings=settings.mail,
        )


async def request_sync(mailbox_id: uuid.UUID) -> bool:
    """Queue a sync of the mailbox; ``False`` if one is already waiting."""
    lock = resource_lock("mailbox", mailbox_id)
    try:
        await sync_mailbox_job.configure(lock=lock, queueing_lock=lock).defer_async(
            mailbox_id=str(mailbox_id)
        )
    except AlreadyEnqueued:
        return False
    return True
