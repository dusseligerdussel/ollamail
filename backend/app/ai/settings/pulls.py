"""Downloading missing models on admin request (``POST /admin/system/models/pull``).

The API records the pull in ``ai_model_pulls`` and queues ``ai.pull_model``; the job
streams the download from Ollama and writes the progress (bytes) to the row, which the
admin page polls. Pulls are never retried automatically: the admin starts them again.
"""

import enum
import time
import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from procrastinate.exceptions import AlreadyEnqueued
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.llm.errors import (
    LLMRequestError,
    LLMTimeoutError,
    LLMUnavailableError,
    ModelNotAvailableError,
)
from app.ai.settings.models import AIModelPull
from app.ai.settings.runtime import worker_gateway
from app.core.config import get_settings
from app.core.db import Database
from app.core.ids import uuid7
from app.core.logging import get_logger
from app.worker import app, resource_lock

log = get_logger(__name__)

PULL_TASK = "ai.pull_model"
# Seconds between two progress writes.
PROGRESS_INTERVAL = 1.0
# A queued or running pull without progress for this long counts as lost (worker gone)
# and may be started again.
STALE_AFTER = timedelta(minutes=10)


class PullStatus(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


def is_active(pull: AIModelPull, now: datetime | None = None) -> bool:
    """Queued or running and not stale."""
    now = now or datetime.now(UTC)
    return pull.status in (PullStatus.QUEUED, PullStatus.RUNNING) and (
        now - pull.updated_at < STALE_AFTER
    )


def pull_error_code(exc: BaseException) -> str:
    if isinstance(exc, ModelNotAvailableError):
        return "model_not_found"
    if isinstance(exc, LLMTimeoutError):
        return "llm_timeout"
    if isinstance(exc, LLMUnavailableError):
        return "llm_unavailable"
    if isinstance(exc, LLMRequestError):
        return "pull_rejected"
    return "pull_failed"


async def list_pulls(session: AsyncSession) -> dict[tuple[str, str], AIModelPull]:
    return {(p.endpoint, p.model): p for p in await session.scalars(select(AIModelPull))}


async def start_pull(session: AsyncSession, endpoint: str, model: str) -> tuple[AIModelPull, bool]:
    """Record a queued pull, or reset a finished or stale one; ``True`` if the caller has
    to queue it (:func:`queue_pull`, after the commit). An active pull stays as it is."""
    inserted = await session.scalar(
        insert(AIModelPull)
        .values(
            id=uuid7(),
            endpoint=endpoint,
            model=model,
            status=PullStatus.QUEUED,
            completed=0,
        )
        .on_conflict_do_nothing(index_elements=["endpoint", "model"])
        .returning(AIModelPull.id)
    )
    pull = await session.scalar(
        select(AIModelPull)
        .where(AIModelPull.endpoint == endpoint, AIModelPull.model == model)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    assert pull is not None
    if inserted is not None:
        return pull, True
    if is_active(pull):
        return pull, False
    pull.status = PullStatus.QUEUED
    pull.completed = 0
    pull.total = None
    pull.error_code = None
    pull.updated_at = datetime.now(UTC)
    await session.flush()
    return pull, True


async def queue_pull(pull_id: uuid.UUID) -> None:
    lock = resource_lock("model_pull", pull_id)
    # Already waiting: the queued job picks up the reset row.
    with suppress(AlreadyEnqueued):
        await pull_model_job.configure(lock=lock, queueing_lock=lock).defer_async(
            pull_id=str(pull_id)
        )


async def _set(database: Database, pull_id: uuid.UUID, **values: object) -> None:
    async with database.sessionmaker() as session:
        await session.execute(
            update(AIModelPull)
            .where(AIModelPull.id == pull_id)
            .values(**values, updated_at=datetime.now(UTC))
        )
        await session.commit()


async def run_pull(database: Database, pull_id: uuid.UUID) -> None:
    async with database.sessionmaker() as session:
        pull = await session.get(AIModelPull, pull_id)
        if pull is None or pull.status == PullStatus.DONE:
            return
        endpoint, model = pull.endpoint, pull.model
    await _set(database, pull_id, status=PullStatus.RUNNING, error_code=None)
    log.info("llm_model_pull_started", endpoint=endpoint, model=model)
    written = 0.0
    completed, total = 0, None
    try:
        async for completed, total in worker_gateway().pull_model(endpoint, model):
            if time.monotonic() - written >= PROGRESS_INTERVAL:
                await _set(database, pull_id, completed=completed, total=total)
                written = time.monotonic()
    except Exception as exc:
        code = pull_error_code(exc)
        await _set(database, pull_id, status=PullStatus.FAILED, error_code=code)
        log.warning("llm_model_pull_failed", endpoint=endpoint, model=model, error_code=code)
        return
    await _set(database, pull_id, status=PullStatus.DONE, completed=completed, total=total)
    log.info("llm_model_pull_finished", endpoint=endpoint, model=model)


@app.task(name=PULL_TASK, queue="default")
async def pull_model_job(pull_id: str) -> None:
    """Download one model (minutes for several GB); progress goes to ``ai_model_pulls``."""
    database = Database(get_settings().database)
    try:
        await run_pull(database, uuid.UUID(pull_id))
    finally:
        await database.dispose()
