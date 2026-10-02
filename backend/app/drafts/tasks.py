"""Housekeeping for reply drafts (data minimisation, docs/PRIVACY.md)."""

from app.core.config import get_settings
from app.core.db import Database
from app.core.logging import get_logger
from app.drafts.service import purge_expired
from app.worker import app

log = get_logger(__name__)


@app.periodic(cron="37 4 * * *", periodic_id="drafts_purge")
@app.task(name="drafts.purge", queue="default", queueing_lock="drafts.purge")
async def purge(timestamp: int) -> None:
    """Daily: delete drafts unchanged for ``OLLAMAIL_DRAFTS_RETENTION_DAYS``."""
    settings = get_settings()
    database = Database(settings.database)
    try:
        async with database.sessionmaker() as db:
            deleted = await purge_expired(db, settings.drafts.retention_days)
            await db.commit()
    finally:
        await database.dispose()
    log.info("drafts_purged", drafts=deleted)
