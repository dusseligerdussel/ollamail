"""Housekeeping for conversations (data minimisation, docs/PRIVACY.md)."""

from app.core.config import get_settings
from app.core.db import Database
from app.core.logging import get_logger
from app.rag.service import purge_expired
from app.worker import app

log = get_logger(__name__)


@app.periodic(cron="23 4 * * *", periodic_id="rag_purge_conversations")
@app.task(
    name="rag.purge_conversations",
    queue="default",
    queueing_lock="rag.purge_conversations",
)
async def purge_conversations(timestamp: int) -> None:
    """Daily: delete conversations unused for ``OLLAMAIL_RAG_HISTORY_RETENTION_DAYS``."""
    settings = get_settings()
    database = Database(settings.database)
    try:
        async with database.sessionmaker() as db:
            deleted = await purge_expired(db, settings.rag.history_retention_days)
            await db.commit()
    finally:
        await database.dispose()
    log.info("rag_conversations_purged", conversations=deleted)
