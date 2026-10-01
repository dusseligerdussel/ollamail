"""Pipeline jobs: plan a message, run its steps in dependency order, re-run outdated ones.

Flow for one message::

    enqueue_processing(message_id)            # called by the mail sync after the commit
      └─ processing.plan_message             # queue "default": create/reset step rows
           └─ processing.run_step(step=...)  # queue of the step, once per ready step
                └─ processing.run_step(...)  # successors, once their dependencies are done

Every job argument is an ID or a step name. Jobs are idempotent: a step that is already
done (at its current version) is skipped, so duplicates and retries are harmless.

Priorities: the priority given to ``enqueue_processing`` is inherited by all step jobs of
that message. Workers always take the job with the highest priority first, so new mails
(``Priority.NEW``) overtake the backlog of an initial import (``Priority.BACKFILL``) and
reprocessing (``Priority.REPROCESS``) on every queue.
"""

import contextlib
import enum
import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from uuid import UUID

from procrastinate import BaseRetryStrategy, JobContext, RetryDecision
from procrastinate.exceptions import AlreadyEnqueued
from procrastinate.jobs import Job

from app.core.config import get_settings
from app.core.db import Database
from app.core.logging import get_logger
from app.processing import service
from app.processing.steps import ProcessingStep, StepContext, StepError, registry
from app.worker import DEFAULT_RETRY, app, resource_lock

log = get_logger(__name__)

# Word boundaries in CamelCase class names (``LLMUnavailableError`` → llm, unavailable, error).
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


class Priority(enum.IntEnum):
    """Job priority of a message's processing; higher runs first."""

    # A mail that just arrived.
    NEW = 10
    # The initial import of a mailbox.
    BACKFILL = 0
    # Re-processing (CLI, version bump of a step).
    REPROCESS = -10


_database: Database | None = None


def get_database() -> Database:
    """Database of the worker process, created on first use."""
    global _database
    if _database is None:
        _database = Database(get_settings().database)
    return _database


@contextmanager
def use_database(database: Database) -> Iterator[None]:
    """Run jobs against ``database`` (tests)."""
    global _database
    saved, _database = _database, database
    try:
        yield
    finally:
        _database = saved


def _message_lock(message_id: UUID | str) -> str:
    return resource_lock("message", message_id)


def _step_lock(message_id: UUID | str, step: str) -> str:
    return resource_lock("message_step", f"{message_id}:{step}")


def error_code(exc: BaseException) -> str:
    """Machine-readable code of a failure: ``StepError.code`` or the snake-cased class
    name. Never the exception text (it may contain mail content)."""
    if isinstance(exc, StepError):
        return exc.code
    return _CAMEL.sub("_", type(exc).__name__).lower()[:64]


class StepRetry(BaseRetryStrategy):
    """Retries a step job with the ``retry`` strategy of its step."""

    def get_retry_decision(self, *, exception: BaseException, job: Job) -> RetryDecision | None:
        if isinstance(exception, StepError) and exception.permanent:
            return None
        step = registry.get(str(job.task_kwargs.get("step")))
        strategy = step.retry if step is not None else DEFAULT_RETRY
        return strategy.get_retry_decision(exception=exception, job=job)


async def enqueue_processing(message_id: UUID, *, priority: Priority = Priority.NEW) -> None:
    """Queue the processing of a stored message (entry point for the mail sync).

    Call it after the message has been committed. Calling it again for the same message
    is harmless: only outdated or missing steps run. ``priority`` is ``Priority.NEW`` for
    mails that just arrived and ``Priority.BACKFILL`` for the initial import.
    """
    if not get_settings().processing.enabled:
        return
    lock = _message_lock(message_id)
    with contextlib.suppress(AlreadyEnqueued):
        await plan_message.configure(
            priority=int(priority), lock=lock, queueing_lock=lock
        ).defer_async(message_id=str(message_id))


async def _defer_steps(message_id: str, names: Sequence[str], priority: int) -> None:
    for name in names:
        step = registry.get(name)
        if step is None:
            continue
        lock = _step_lock(message_id, name)
        with contextlib.suppress(AlreadyEnqueued):
            await run_step.configure(
                queue=step.queue, priority=priority, lock=lock, queueing_lock=lock
            ).defer_async(message_id=message_id, step=name)


@app.task(name="processing.plan_message", queue="default", retry=DEFAULT_RETRY, pass_context=True)
async def plan_message(context: JobContext, message_id: str) -> None:
    """Create or reset the step rows of a message and queue the steps that can run."""
    steps = registry.ordered()
    async with get_database().sessionmaker() as session:
        ready = await service.plan(session, UUID(message_id), steps)
        await session.commit()
    await _defer_steps(message_id, ready, _priority(context))


def _priority(context: JobContext) -> int:
    return context.job.priority if context.job is not None else int(Priority.NEW)


@app.task(name="processing.run_step", queue="default", retry=StepRetry(), pass_context=True)
async def run_step(context: JobContext, message_id: str, step: str) -> None:
    """Run one step for one message, then queue the steps that became ready."""
    definition = registry.get(step)
    if definition is None:
        log.warning("processing_step_unknown", message_id=message_id, step=step)
        return
    steps = registry.ordered()
    database = get_database()
    message_uuid = UUID(message_id)

    async with database.sessionmaker() as session:
        mailbox_id = await service.start_step(session, message_uuid, definition)
        await session.commit()
    if mailbox_id is None:
        return

    try:
        async with database.sessionmaker() as session:
            await definition.handler(StepContext(session, message_uuid, mailbox_id))
            ready = await service.finish_step(session, message_uuid, mailbox_id, definition, steps)
            await session.commit()
    except Exception as exc:
        await _record_failure(context, database, message_uuid, mailbox_id, definition, exc)
        if isinstance(exc, StepError) and exc.permanent:
            return
        raise
    log.info("processing_step_done", message_id=message_id, step=step)
    await _defer_steps(message_id, ready, _priority(context))


async def _record_failure(
    context: JobContext,
    database: Database,
    message_id: UUID,
    mailbox_id: UUID,
    step: ProcessingStep,
    exc: Exception,
) -> None:
    code = error_code(exc)
    final = (
        context.job is None
        or StepRetry().get_retry_decision(exception=exc, job=context.job) is None
    )
    async with database.sessionmaker() as session:
        await service.fail_step(session, message_id, mailbox_id, step, code, final=final)
        await session.commit()
    log.warning(
        "processing_step_failed",
        message_id=str(message_id),
        step=step.name,
        error_code=code,
        final=final,
    )


async def requeue_messages(message_ids: Sequence[UUID], priority: Priority) -> None:
    for message_id in message_ids:
        await enqueue_processing(message_id, priority=priority)


@app.periodic(cron="*/10 * * * *", periodic_id="processing_requeue_outdated")
@app.task(
    name="processing.requeue_outdated",
    queue="default",
    queueing_lock="processing.requeue_outdated",
)
async def requeue_outdated(timestamp: int) -> None:
    """Every 10 minutes: queue messages that were never processed or whose steps have a
    newer version, newest first and behind all other work (``Priority.REPROCESS``)."""
    settings = get_settings().processing
    steps = registry.ordered()
    if not settings.enabled or not steps:
        return
    async with get_database().sessionmaker() as session:
        message_ids = await service.outdated_messages(
            session, steps, limit=settings.requeue_batch_size
        )
    await requeue_messages(message_ids, Priority.REPROCESS)
    if message_ids:
        log.info("processing_requeued", count=len(message_ids))
