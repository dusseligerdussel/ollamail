"""Todo export jobs (listed in ``app.worker.TASK_MODULES``).

* ``todos.export_schedule`` (periodic, every minute, queue ``default``): queues a sync for
  every target with new, changed or deleted todos, or with a status check due.
* ``todos.export_sync`` (queue ``sync``): one sync run for one target
  (``app.todos.export.service.sync_target``).

Job arguments are target IDs only. ``lock`` serialises the runs of one target.
"""

import contextlib
import uuid
from datetime import UTC, datetime

from procrastinate.exceptions import AlreadyEnqueued

from app.core.config import get_settings
from app.processing.tasks import get_database
from app.todos.export import service
from app.todos.export.registry import create_sink
from app.worker import DEFAULT_RETRY, app, resource_lock


def get_sink_builder() -> service.SinkBuilder:
    """Creates the sink of a target (tests replace it)."""
    return create_sink


def _lock(target_id: uuid.UUID | str) -> str:
    return resource_lock("todo_export", target_id)


async def enqueue_sync(target_id: uuid.UUID) -> None:
    """Queue a sync of a committed target; a second call while one waits is a no-op."""
    with contextlib.suppress(AlreadyEnqueued):
        await export_sync.configure(
            lock=_lock(target_id), queueing_lock=_lock(target_id)
        ).defer_async(target_id=str(target_id))


@app.periodic(cron="* * * * *", periodic_id="todo_export_schedule")
@app.task(
    name="todos.export_schedule",
    queue="default",
    queueing_lock="todos.export_schedule",
    lock="todos.export_schedule",
    retry=DEFAULT_RETRY,
)
async def export_schedule(timestamp: int) -> None:
    settings = get_settings().todos
    now = datetime.fromtimestamp(timestamp, UTC)
    async with get_database().sessionmaker() as session:
        due = await service.due_targets(session, settings, now=now)
    for target_id in due:
        await enqueue_sync(target_id)


@app.task(name="todos.export_sync", queue="sync", retry=DEFAULT_RETRY)
async def export_sync(target_id: str) -> None:
    settings = get_settings()
    async with get_database().sessionmaker() as session:
        # Target errors are stored on the target (shown in the settings) and retried by
        # the schedule; they do not fail the job.
        await service.sync_target(
            session,
            uuid.UUID(target_id),
            settings=settings.todos,
            public_url=settings.auth.public_url,
            sink_builder=get_sink_builder(),
        )
