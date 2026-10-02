"""Privacy jobs.

* ``privacy.export`` (queue ``default``): builds the ZIP of one export (argument: its ID).
* ``privacy.retention`` (periodic, daily): enforces the retention periods.
* ``privacy.cleanup_exports`` (periodic, hourly): deletes expired exports and their files.
"""

import contextlib
import uuid
from datetime import UTC, datetime

from procrastinate.exceptions import AlreadyEnqueued

from app.core.config import get_settings
from app.core.logging import get_logger
from app.digest.storage import DigestStorage
from app.privacy import exports, retention
from app.privacy.storage import ExportStorage
from app.processing.tasks import get_database
from app.worker import DEFAULT_RETRY, app, resource_lock

log = get_logger(__name__)


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
