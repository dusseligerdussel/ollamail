"""Housekeeping for auth data (data minimisation, docs/PRIVACY.md)."""

from datetime import timedelta

from app.auth import rate_limit
from app.auth.sessions import purge_expired_sessions
from app.core.config import get_settings
from app.core.db import Database
from app.core.logging import get_logger
from app.worker import app

log = get_logger(__name__)


@app.periodic(cron="41 * * * *", periodic_id="auth_cleanup")
@app.task(name="auth.cleanup", queue="default", queueing_lock="auth.cleanup")
async def cleanup(timestamp: int) -> None:
    """Hourly: delete expired/idle sessions and finished rate-limit windows."""
    settings = get_settings()
    database = Database(settings.database)
    try:
        async with database.sessionmaker() as db:
            sessions = await purge_expired_sessions(db, settings.auth)
            window = timedelta(minutes=settings.auth.login_window_minutes)
            counters = await rate_limit.purge(db, window)
            await db.commit()
    finally:
        await database.dispose()
    log.info("auth_cleanup_finished", sessions=sessions, rate_limits=counters)
