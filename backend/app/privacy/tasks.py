"""Privacy jobs.

* ``privacy.export`` (queue ``default``): builds the ZIP of one export (argument: its ID).
* ``privacy.retention`` (periodic, daily): enforces the retention periods.
* ``privacy.cleanup_exports`` (periodic, hourly): deletes expired exports and their files.
* ``privacy.delete_user`` (queue ``default``): finishes the deletion of a user once their
  mailboxes are removed (``app.privacy.deletion``, #177); queued by the request, after
  each removed mailbox of the user and by ``privacy.resume_user_deletions`` (periodic,
  every 15 minutes) for jobs that got lost.
"""

import contextlib
import uuid
from datetime import UTC, datetime

from procrastinate.exceptions import AlreadyEnqueued

from app.core.config import get_settings
from app.core.logging import get_logger
from app.digest.storage import DigestStorage
from app.mail import deletion as mail_deletion
from app.mail.hooks import MailboxDeleted, on_mailbox_deleted
from app.privacy import deletion, exports, retention
from app.privacy.storage import ExportStorage
from app.processing.tasks import get_database
from app.worker import DEFAULT_RETRY, app, resource_lock

log = get_logger(__name__)

DELETE_USER_TASK = "privacy.delete_user"


def export_storage() -> ExportStorage:
    return ExportStorage(get_settings().storage.data_dir)


async def enqueue_export(export_id: uuid.UUID) -> None:
    """Queue a committed export; a second call while it waits is a no-op."""
    lock = resource_lock("export", export_id)
    with contextlib.suppress(AlreadyEnqueued):
        await build_export.configure(lock=lock, queueing_lock=lock).defer_async(
            export_id=str(export_id)
        )


@app.task(name="privacy.export", queue="default", retry=DEFAULT_RETRY)
async def build_export(export_id: str) -> None:
    settings = get_settings()
    async with get_database().sessionmaker() as session:
        await exports.run_export(
            session,
            uuid.UUID(export_id),
            storage=ExportStorage(settings.storage.data_dir),
            digest_storage=DigestStorage(settings.storage.data_dir),
            expiry_hours=settings.privacy.export_expiry_hours,
        )


@app.periodic(cron="7 3 * * *", periodic_id="privacy_retention")
@app.task(
    name="privacy.retention",
    queue="default",
    queueing_lock="privacy.retention",
    lock="privacy.retention",
    retry=DEFAULT_RETRY,
)
async def enforce_retention(timestamp: int) -> None:
    """Daily: hard-delete data older than the retention periods (with files)."""
    async with get_database().sessionmaker() as session:
        await retention.run(session, get_settings(), now=datetime.fromtimestamp(timestamp, UTC))


@app.periodic(cron="53 * * * *", periodic_id="privacy_cleanup_exports")
@app.task(
    name="privacy.cleanup_exports",
    queue="default",
    queueing_lock="privacy.cleanup_exports",
    lock="privacy.cleanup_exports",
    retry=DEFAULT_RETRY,
)
async def cleanup_exports(timestamp: int) -> None:
    """Hourly: delete expired exports and export files without an export."""
    async with get_database().sessionmaker() as session:
        await exports.cleanup_exports(
            session, export_storage(), now=datetime.fromtimestamp(timestamp, UTC)
        )


def file_stores() -> deletion.FileStores:
    data_dir = get_settings().storage.data_dir
    return deletion.FileStores(digests=DigestStorage(data_dir), exports=ExportStorage(data_dir))


@app.task(name=DELETE_USER_TASK, queue="default", retry=DEFAULT_RETRY)
async def delete_user_job(user_id: str) -> None:
    async with get_database().sessionmaker() as session:
        result = await deletion.purge_user(session, uuid.UUID(user_id), file_stores())
    # Usually queued already; queues them again if their jobs got lost.
    for mailbox_id in result.pending_mailboxes:
        await mail_deletion.defer_deletion(mailbox_id)


async def defer_user_deletion(user_id: uuid.UUID) -> bool:
    """Queue ``privacy.delete_user``; ``False`` if one is already waiting."""
    lock = resource_lock("user_deletion", user_id)
    try:
        await delete_user_job.configure(lock=lock, queueing_lock=lock).defer_async(
            user_id=str(user_id)
        )
    except AlreadyEnqueued:
        return False
    return True


@on_mailbox_deleted
async def continue_user_deletion(event: MailboxDeleted) -> None:
    """A mailbox of a user being deleted is gone: delete the user if it was the last."""
    if event.owner_user_id is None:
        return
    async with get_database().sessionmaker() as session:
        pending = await deletion.pending_user_deletions(session, [event.owner_user_id])
    if pending:
        await defer_user_deletion(event.owner_user_id)


@app.periodic(cron="*/15 * * * *", periodic_id="privacy.resume_user_deletions")
@app.task(
    name="privacy.resume_user_deletions",
    queue="default",
    queueing_lock="privacy.resume_user_deletions",
    lock="privacy.resume_user_deletions",
    retry=DEFAULT_RETRY,
)
async def resume_user_deletions(timestamp: int) -> None:
    """Queue user deletions whose job got lost (queueing failed after the request
    committed, or the job failed for good). Waiting ones are left alone."""
    async with get_database().sessionmaker() as session:
        user_ids = await deletion.pending_user_deletions(session)
    queued = 0
    for user_id in user_ids:
        queued += await defer_user_deletion(user_id)
    if queued:
        log.info("privacy_user_deletions_resumed", count=queued)
