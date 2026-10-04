"""Prometheus endpoint of the api (``/metrics``, ``OLLAMAIL_METRICS_*``, off by default).

Serves the api's process metrics (``app.core.metrics``) and the database metrics, which only
the api reports so they are not duplicated per worker: queue depth per queue, priority and
status, failed jobs, processing steps per step and per mailbox (``count_steps_by_mailbox``)
and the sync state per mailbox (``statuses``, as on the system status page). They are read
at most every ``OLLAMAIL_METRICS_DATABASE_REFRESH_SECONDS``, as the queries scan the job and
step tables.

Privacy (docs/PRIVACY.md): mailboxes appear as IDs, errors as codes; never content,
subjects, addresses or display names. The frontend proxy does not forward ``/api/metrics``;
with ``OLLAMAIL_METRICS_TOKEN`` scrapers must also send ``Authorization: Bearer <token>``.
"""

import asyncio
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field

from fastapi import APIRouter, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, CollectorRegistry, generate_latest
from prometheus_client.core import GaugeMetricFamily, Metric
from prometheus_client.registry import Collector
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import MetricsSettings, Settings
from app.core.db import Database
from app.core.logging import get_logger
from app.core.metrics import authorized
from app.mail.api.service import statuses
from app.mail.models import Mailbox
from app.processing import service as processing
from app.processing.models import OPEN_STEPS, MessageProcessing, StepStatus

log = get_logger(__name__)
router = APIRouter(tags=["health"])


async def _queue_metrics(session: AsyncSession) -> list[Metric]:
    depth = GaugeMetricFamily(
        "ollamail_queue_jobs",
        "Waiting (todo) and running (doing) jobs per queue and priority.",
        labels=["queue", "priority", "status"],
    )
    for queue, priority, status, count in await session.execute(
        text(
            "SELECT queue_name, priority, status::text, count(*) FROM procrastinate_jobs "
            "WHERE status IN ('todo', 'doing') GROUP BY queue_name, priority, status"
        )
    ):
        depth.add_metric([queue, str(priority), status], count)
    failed = GaugeMetricFamily(
        "ollamail_queue_failed_jobs",
        "Failed jobs per queue and task that are still kept (deleted after "
        "OLLAMAIL_WORKER_FAILED_JOB_RETENTION_HOURS, default 7 days).",
        labels=["queue", "task"],
    )
    for queue, task, count in await session.execute(
        text(
            "SELECT queue_name, task_name, count(*) FROM procrastinate_jobs "
            "WHERE status = 'failed' GROUP BY queue_name, task_name"
        )
    ):
        failed.add_metric([queue, task], count)
    return [depth, failed]


async def _processing_metrics(session: AsyncSession) -> list[Metric]:
    steps = GaugeMetricFamily(
        "ollamail_processing_steps",
        "Processing steps that are pending, running or failed, per step.",
        labels=["step", "status"],
    )
    for step, status, count in await session.execute(
        select(MessageProcessing.step, MessageProcessing.status, func.count())
        .where(text(OPEN_STEPS))
        .group_by(MessageProcessing.step, MessageProcessing.status)
    ):
        steps.add_metric([step, StepStatus(status).value], count)
    per_mailbox = GaugeMetricFamily(
        "ollamail_mailbox_processing_steps",
        "Processing steps per mailbox; retry_scheduled is the part of failed that runs "
        "again automatically.",
        labels=["mailbox_id", "status"],
    )
    for mailbox_id, counts in (
        await processing.count_steps_by_mailbox(session, skipped=False)
    ).items():
        for name in ("pending", "running", "failed", "retry_scheduled"):
            per_mailbox.add_metric([str(mailbox_id), name], getattr(counts, name))
    return [steps, per_mailbox]


async def _sync_metrics(session: AsyncSession) -> list[Metric]:
    phase = GaugeMetricFamily(
        "ollamail_mailbox_sync_phase",
        "1 for the current sync phase of each mailbox.",
        labels=["mailbox_id", "phase"],
    )
    error = GaugeMetricFamily(
        "ollamail_mailbox_sync_error",
        "1 if the last sync of the mailbox failed, labelled with the error code.",
        labels=["mailbox_id", "code"],
    )
    failed_folders = GaugeMetricFamily(
        "ollamail_mailbox_sync_failed_folders",
        "Folders of the mailbox whose last sync failed.",
        labels=["mailbox_id"],
    )
    last_synced = GaugeMetricFamily(
        "ollamail_mailbox_last_sync_timestamp_seconds",
        "Unix time of the last complete sync of the mailbox.",
        labels=["mailbox_id"],
    )
    mailboxes = list(await session.scalars(select(Mailbox).order_by(Mailbox.id)))
    for mailbox_id, status in (await statuses(session, mailboxes)).items():
        mailbox = str(mailbox_id)
        phase.add_metric([mailbox, status.phase], 1)
        if status.last_error:
            error.add_metric([mailbox, status.last_error], 1)
        failed_folders.add_metric([mailbox], status.folders_failed)
        if status.last_synced_at is not None:
            last_synced.add_metric([mailbox], status.last_synced_at.timestamp())
    return [phase, error, failed_folders, last_synced]


async def collect_database_metrics(session: AsyncSession) -> list[Metric]:
    return [
        *await _queue_metrics(session),
        *await _processing_metrics(session),
        *await _sync_metrics(session),
    ]


class _Fixed(Collector):
    def __init__(self, metrics: Iterable[Metric]) -> None:
        self._metrics = list(metrics)

    def collect(self) -> Iterator[Metric]:
        return iter(self._metrics)


@dataclass
class DatabaseMetrics:
    """Database metrics rendered in the text format, refreshed at most every ``max_age``."""

    database: Database
    max_age: float
    clock: Callable[[], float] = time.monotonic
    _body: bytes = b""
    _read_at: float | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def render(self) -> bytes:
        async with self._lock:
            now = self.clock()
            if self._read_at is None or now - self._read_at >= self.max_age:
                self._body = await self._read()
                self._read_at = now
            return self._body

    async def _read(self) -> bytes:
        up = GaugeMetricFamily(
            "ollamail_metrics_database_up", "1 if the database metrics could be read."
        )
        metrics: list[Metric] = []
        try:
            async with self.database.sessionmaker() as session:
                metrics = await collect_database_metrics(session)
            up.add_metric([], 1)
        except Exception as exc:
            # Type only: driver messages may contain connection details.
            log.warning("metrics_database_failed", error_type=type(exc).__name__)
            up.add_metric([], 0)
        registry = CollectorRegistry(auto_describe=False)
        registry.register(_Fixed([up, *metrics]))
        return generate_latest(registry)


def _database_metrics(request: Request) -> DatabaseMetrics:
    state = request.app.state
    cache: DatabaseMetrics | None = getattr(state, "database_metrics", None)
    if cache is None:
        settings: Settings = state.settings
        cache = DatabaseMetrics(state.database, settings.metrics.database_refresh_seconds)
        state.database_metrics = cache
    return cache


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    """Prometheus metrics of the api process and the database (404 while disabled)."""
    settings: MetricsSettings = request.app.state.settings.metrics
    if not settings.enabled:
        return Response(status_code=404)
    if not authorized(request.headers.get("authorization"), settings.token):
        return Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})
    body = generate_latest(REGISTRY) + await _database_metrics(request).render()
    return Response(body, media_type=CONTENT_TYPE_LATEST)
