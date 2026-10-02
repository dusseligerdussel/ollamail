"""Background worker: Procrastinate app, queues and task conventions.

Run with ``python -m app.worker`` (the ``worker`` service in ``deploy/compose.yaml``).
The job tables live in PostgreSQL; their schema is an Alembic migration
(``migrations/versions/*_procrastinate_schema.py``), never ``procrastinate schema --apply``.

Queues
    ``sync`` (mail provider I/O), ``llm`` (LLM calls), ``tts`` (speech synthesis) and
    ``default`` (everything else, periodic housekeeping). ``OLLAMAIL_WORKER_QUEUES``
    selects the queues of one worker process, so e.g. a second container can run only
    ``llm``. The ``llm`` queue runs with its own parallelism (``OLLAMAIL_LLM_MAX_CONCURRENCY``
    job slots, of which the LLM gateway lets the admin-set ``concurrency`` call the model at
    once) so CPU-only hosts are not overloaded; all other queues share
    ``OLLAMAIL_WORKER_CONCURRENCY``.

Task conventions
    * Register tasks with ``@app.task(name="<module>.<action>", queue=..., retry=DEFAULT_RETRY)``
      in the feature module and add that module to ``TASK_MODULES``.
    * Tasks are **idempotent**: a retry or a duplicate defer must not change the result.
    * Arguments are **IDs only** (UUIDs as strings, ints), never mail content, addresses
      or credentials. Job arguments are stored in the database and visible to admins.
    * Failures are retried with exponential backoff (``DEFAULT_RETRY``).
    * Work on one resource is serialised with a lock key, e.g.
      ``sync_mailbox.configure(lock=resource_lock("mailbox", mailbox_id),
      queueing_lock=resource_lock("mailbox", mailbox_id)).defer_async(...)``.
      ``lock`` prevents two jobs for the same mailbox from running at once,
      ``queueing_lock`` prevents queueing a second one while one is still waiting.
    * Periodic (cron) tasks use ``@app.periodic(cron=..., periodic_id=...)`` on top of
      ``@app.task``; the worker defers them, duplicates across workers are de-duplicated
      in the database.
    * Progress for the UI is published with ``app.core.events.publish``.
"""

import asyncio
import importlib
import signal
import sys
from dataclasses import dataclass
from typing import get_args
from uuid import UUID

import procrastinate
from procrastinate import JobContext, RetryStrategy

from app.core.config import DatabaseSettings, QueueName, Settings, get_settings
from app.core.db import libpq_url
from app.core.logging import configure_logging, get_logger

if __name__ == "__main__":
    # ``python -m app.worker`` executes this file as ``__main__``. Task modules import
    # ``app.worker``, so hand over to that module before a second App is created here.
    sys.exit(importlib.import_module("app.worker").cli())

log = get_logger(__name__)

QUEUES: tuple[QueueName, ...] = get_args(QueueName)

# Modules that define tasks; the worker imports them on start-up. Add one line per module.
TASK_MODULES: list[str] = [
    "app.ai.tts.tasks",
    "app.auth.tasks",
    "app.digest.tasks",
    "app.mail.sync.tasks",
    "app.processing.tasks",
    "app.triage.tasks",
    "app.search.tasks",
    "app.todos.steps",
    "app.rag.tasks",
]

# Waits 2, 4, 8, ... 128 seconds between attempts (8 attempts, ~4 minutes in total).
DEFAULT_RETRY = RetryStrategy(max_attempts=8, exponential_wait=2)

# Finished jobs (and their arguments) are deleted after this many hours.
JOB_RETENTION_HOURS = 7 * 24


def resource_lock(kind: str, resource_id: UUID | str) -> str:
    """Lock key that serialises all jobs touching one resource, e.g. one mailbox."""
    return f"{kind}:{resource_id}"


def build_connector(settings: DatabaseSettings, max_size: int = 4) -> procrastinate.BaseConnector:
    # Connections are opened lazily by ``App.open_async``.
    return procrastinate.PsycopgConnector(
        conninfo=libpq_url(settings),
        min_size=1,
        max_size=max_size,
        kwargs={"connect_timeout": max(1, round(settings.connect_timeout))},
    )


app = procrastinate.App(
    connector=build_connector(get_settings().database),
    import_paths=TASK_MODULES,
)


@app.periodic(cron="17 3 * * *", periodic_id="remove_old_jobs")
@app.task(
    name="worker.remove_old_jobs",
    queue="default",
    queueing_lock="worker.remove_old_jobs",
    pass_context=True,
)
async def remove_old_jobs(context: JobContext, timestamp: int) -> None:
    """Daily: delete finished jobs and their events (data minimisation)."""
    await context.app.job_manager.delete_old_jobs(
        nb_hours=JOB_RETENTION_HOURS,
        include_failed=True,
        include_cancelled=True,
        include_aborted=True,
    )


class WorkerCrashedError(Exception):
    """A worker stopped without being asked to."""


@dataclass(frozen=True)
class WorkerGroup:
    """One Procrastinate worker: a set of queues sharing one concurrency limit."""

    name: str
    queues: tuple[QueueName, ...]
    concurrency: int


def worker_groups(settings: Settings) -> list[WorkerGroup]:
    queues = tuple(q for q in QUEUES if q in settings.worker.queues)
    groups = []
    shared = tuple(q for q in queues if q != "llm")
    if shared:
        groups.append(WorkerGroup("main", shared, settings.worker.concurrency))
    if "llm" in queues:
        # Slots up to the maximum; the gateway enforces the admin setting (app/ai/settings).
        slots = max(settings.llm.concurrency, settings.llm.max_concurrency)
        groups.append(WorkerGroup("llm", ("llm",), slots))
    return groups


def pool_size(groups: list[WorkerGroup]) -> int:
    # Per worker: one job connection per slot plus listener and heartbeat/fetch connections.
    return sum(group.concurrency + 2 for group in groups) + 1


def background_services(settings: Settings, stop: asyncio.Event) -> list[asyncio.Task[None]]:
    """Long-running tasks next to the job workers, e.g. the mailbox push watcher."""
    services = []
    if "sync" in settings.worker.queues and settings.mail.watch_enabled:
        from app.mail.sync.watcher import run_watcher

        services.append(asyncio.create_task(run_watcher(settings, stop), name="mail-watcher"))
    return services


async def run(settings: Settings, stop: asyncio.Event) -> None:
    """Run all worker groups until ``stop`` is set, then shut down gracefully."""
    groups = worker_groups(settings)
    with app.replace_connector(build_connector(settings.database, pool_size(groups))):
        async with app.open_async():
            workers = [
                asyncio.create_task(
                    app.run_worker_async(
                        name=group.name,
                        queues=list(group.queues),
                        concurrency=group.concurrency,
                        shutdown_graceful_timeout=settings.worker.shutdown_timeout,
                        install_signal_handlers=False,
                    ),
                    name=f"worker-{group.name}",
                )
                for group in groups
            ]
            services = background_services(settings, stop)
            log.info(
                "worker_started",
                groups={group.name: list(group.queues) for group in groups},
            )
            stopping = asyncio.create_task(stop.wait())
            await asyncio.wait([stopping, *workers], return_when=asyncio.FIRST_COMPLETED)
            stopping.cancel()
            for service in services:
                service.cancel()
            await asyncio.gather(*services, return_exceptions=True)
            # Cancelling a Procrastinate worker stops it gracefully: no new jobs are
            # fetched, running jobs get ``shutdown_timeout`` seconds to finish.
            for worker in workers:
                worker.cancel()
            results = await asyncio.gather(*workers, return_exceptions=True)
    failed = [r for r in results if not isinstance(r, asyncio.CancelledError | type(None))]
    for error in failed:
        log.error("worker_crashed", error_type=type(error).__name__)
    if failed:
        raise WorkerCrashedError
    log.info("worker_stopped")


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.logging)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await run(settings, stop)


def cli() -> int:
    try:
        asyncio.run(main())
    except WorkerCrashedError:
        return 1
    return 0
