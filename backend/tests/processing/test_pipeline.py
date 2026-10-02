"""Integration tests: the pipeline with dummy steps, a real worker and PostgreSQL."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import asyncpg  # type: ignore[import-untyped]
import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from app.cli import cli
from app.core.config import get_settings
from app.core.db import libpq_url
from app.core.events import CHANNEL
from app.processing import service
from app.processing.models import MessageProcessing, StepStatus
from app.processing.steps import StepError, registry
from app.processing.tasks import Priority, enqueue_processing, requeue_outdated, run_step
from app.worker import app
from tests.processing.conftest import Pipeline, Recorder

pytestmark = pytest.mark.db


async def _enqueue(*message_ids: uuid.UUID, priority: Priority = Priority.NEW) -> None:
    async with app.open_async():
        for message_id in message_ids:
            await enqueue_processing(message_id, priority=priority)


async def _rows(pipeline: Pipeline, message_id: uuid.UUID) -> dict[str, MessageProcessing]:
    async with pipeline.database.sessionmaker() as session:
        rows = await session.scalars(
            select(MessageProcessing).where(MessageProcessing.message_id == message_id)
        )
        return {row.step: row for row in rows}


def _register(recorder: Recorder, *steps: tuple[str, tuple[str, ...]]) -> None:
    for name, depends_on in steps:
        registry.register(recorder.step(name, depends_on=depends_on))


@pytest.fixture
async def notifications(pipeline: Pipeline) -> AsyncIterator[list[dict[str, object]]]:
    """Raw payloads published on the event channel while the test runs."""
    received: list[dict[str, object]] = []
    connection = await asyncpg.connect(libpq_url(pipeline.settings))
    await connection.add_listener(CHANNEL, lambda *args: received.append(json.loads(args[3])))
    yield received
    await connection.close()


async def test_drain_runs_only_the_jobs_of_the_test(pipeline: Pipeline, recorder: Recorder) -> None:
    # Forget earlier defers: a real worker would now queue every periodic task that came
    # due in the last ten minutes (``digest.schedule`` at least, it runs every minute).
    await pipeline.execute("DELETE FROM procrastinate_periodic_defers")
    _register(recorder, ("normalize", ()))
    message_id = await pipeline.add_message()

    await _enqueue(message_id)
    await pipeline.drain()

    tasks = await pipeline.execute("SELECT DISTINCT task_name FROM procrastinate_jobs")
    assert sorted(name for (name,) in tasks) == ["processing.plan_message", "processing.run_step"]
    assert await pipeline.execute("SELECT 1 FROM procrastinate_periodic_defers") == []


async def test_steps_run_in_dependency_order(
    pipeline: Pipeline, recorder: Recorder, notifications: list[dict[str, object]]
) -> None:
    registry.register(recorder.step("todos", depends_on=("triage",), queue="llm"))
    registry.register(recorder.step("index", depends_on=("normalize",)))
    registry.register(recorder.step("triage", depends_on=("normalize",), queue="llm"))
    registry.register(recorder.step("normalize"))
    message_id = await pipeline.add_message()

    await _enqueue(message_id)
    await pipeline.drain()

    names = recorder.names()
    assert sorted(names) == ["index", "normalize", "todos", "triage"]
    assert names[0] == "normalize"
    assert names.index("triage") < names.index("todos")
    rows = await _rows(pipeline, message_id)
    assert {name: (row.status, row.version) for name, row in rows.items()} == {
        name: (StepStatus.DONE, 1) for name in names
    }
    await asyncio.sleep(0.2)
    assert notifications == [
        {
            "user_id": str(pipeline.owner_id),
            "event": {
                "type": "message.processed",
                "ids": {"message_id": str(message_id), "mailbox_id": str(pipeline.mailbox_id)},
                "status": "done",
            },
        }
    ]


async def test_join_step_runs_once_after_parallel_dependencies(
    pipeline: Pipeline, recorder: Recorder
) -> None:
    _register(recorder, ("a", ()), ("b", ()), ("c", ()), ("join", ("a", "b", "c")))
    message_ids = [await pipeline.add_message() for _ in range(5)]

    await _enqueue(*message_ids)
    await pipeline.drain(concurrency=4)

    for message_id in message_ids:
        names = recorder.names(message_id)
        assert sorted(names) == ["a", "b", "c", "join"]
        assert names[-1] == "join"


async def test_processing_is_idempotent(pipeline: Pipeline, recorder: Recorder) -> None:
    _register(recorder, ("normalize", ()), ("triage", ("normalize",)))
    message_id = await pipeline.add_message()
    await _enqueue(message_id)
    await pipeline.drain()
    assert recorder.names() == ["normalize", "triage"]

    # Enqueued again, and a stray duplicate job of a step that is already done.
    await _enqueue(message_id)
    async with app.open_async():
        await run_step.defer_async(message_id=str(message_id), step="triage")
    await pipeline.drain()

    assert recorder.names() == ["normalize", "triage"]


async def test_failed_attempt_is_retried(pipeline: Pipeline, recorder: Recorder) -> None:
    _register(recorder, ("normalize", ()), ("triage", ("normalize",)))
    recorder.failures["normalize"] = 1
    message_id = await pipeline.add_message()

    await _enqueue(message_id)
    await pipeline.drain()

    assert recorder.names() == ["normalize", "normalize", "triage"]
    row = (await _rows(pipeline, message_id))["normalize"]
    assert (row.status, row.attempts, row.error_code) == (StepStatus.DONE, 2, None)


async def test_exhausted_retries_mark_the_step_failed(
    pipeline: Pipeline, recorder: Recorder, notifications: list[dict[str, object]]
) -> None:
    _register(recorder, ("normalize", ()), ("triage", ("normalize",)))
    recorder.failures["normalize"] = 10
    message_id = await pipeline.add_message()

    await _enqueue(message_id)
    await pipeline.drain()

    # One run plus FAST_RETRY.max_attempts retries; the dependent step never runs.
    assert recorder.names() == ["normalize"] * 3
    rows = await _rows(pipeline, message_id)
    assert (rows["normalize"].status, rows["normalize"].error_code) == (
        StepStatus.FAILED,
        "runtime_error",
    )
    assert rows["triage"].status == StepStatus.PENDING
    # Only codes are stored, never exception texts.
    stored = await pipeline.execute(
        "SELECT count(*) FROM message_processing WHERE error_code LIKE '%example%'"
    )
    assert stored == [(0,)]
    await asyncio.sleep(0.2)
    assert [n["event"] for n in notifications] == [
        {
            "type": "message.processed",
            "ids": {"message_id": str(message_id), "mailbox_id": str(pipeline.mailbox_id)},
            "status": "failed",
        }
    ]


async def test_permanent_error_is_not_retried(pipeline: Pipeline, recorder: Recorder) -> None:
    registry.register(recorder.step("triage", error=StepError("unsupported", permanent=True)))
    recorder.failures["triage"] = 10
    message_id = await pipeline.add_message()

    await _enqueue(message_id)
    await pipeline.drain()

    assert recorder.names() == ["triage"]
    row = (await _rows(pipeline, message_id))["triage"]
    assert (row.status, row.error_code) == (StepStatus.FAILED, "unsupported")
    jobs = await pipeline.execute(
        "SELECT status FROM procrastinate_jobs WHERE task_name = 'processing.run_step'"
    )
    assert jobs == [("succeeded",)]


async def test_version_bump_reprocesses_only_that_step(
    pipeline: Pipeline, recorder: Recorder
) -> None:
    _register(recorder, ("normalize", ()), ("triage", ("normalize",)), ("todos", ("triage",)))
    message_id = await pipeline.add_message()
    await _enqueue(message_id)
    await pipeline.drain()
    recorder.calls.clear()

    with registry.isolated(
        recorder.step("normalize"),
        recorder.step("triage", version=2, depends_on=("normalize",)),
        recorder.step("todos", depends_on=("triage",)),
    ):
        async with app.open_async():
            await requeue_outdated(timestamp=0)
        await pipeline.drain()
        rows = await _rows(pipeline, message_id)

    assert recorder.names() == ["triage"]
    assert {name: (row.status, row.version) for name, row in rows.items()} == {
        "normalize": (StepStatus.DONE, 1),
        "triage": (StepStatus.DONE, 2),
        "todos": (StepStatus.DONE, 1),
    }


async def test_backfill_does_not_block_new_mail(pipeline: Pipeline, recorder: Recorder) -> None:
    registry.register(recorder.step("normalize"))
    registry.register(recorder.step("triage", depends_on=("normalize",), queue="llm"))
    backlog = [await pipeline.add_message() for _ in range(10)]
    await _enqueue(*backlog, priority=Priority.BACKFILL)
    new_mail = await pipeline.add_message()
    await _enqueue(new_mail, priority=Priority.NEW)

    await pipeline.drain()

    # The new mail goes through all of its steps before the first backlog mail starts.
    assert recorder.calls[:2] == [("normalize", new_mail), ("triage", new_mail)]
    assert len(recorder.calls) == 22


async def test_disabled_mailbox_is_skipped_until_enabled(
    pipeline: Pipeline, recorder: Recorder
) -> None:
    _register(recorder, ("normalize", ()))
    message_id = await pipeline.add_message()
    async with pipeline.database.sessionmaker() as session:
        await service.set_mailbox_enabled(session, pipeline.mailbox_id, False)
        await session.commit()

    await _enqueue(message_id)
    await pipeline.drain()
    async with app.open_async():
        await requeue_outdated(timestamp=0)
    await pipeline.drain()
    assert recorder.calls == []
    assert await _rows(pipeline, message_id) == {}

    async with pipeline.database.sessionmaker() as session:
        await service.set_mailbox_enabled(session, pipeline.mailbox_id, True)
        await session.commit()
    async with app.open_async():
        await requeue_outdated(timestamp=0)
    await pipeline.drain()

    assert recorder.names() == ["normalize"]


async def test_deleted_message_is_ignored(pipeline: Pipeline, recorder: Recorder) -> None:
    _register(recorder, ("normalize", ()))

    await _enqueue(uuid.uuid4())
    await pipeline.drain()

    assert recorder.calls == []


async def test_deleting_the_mailbox_removes_processing_rows(
    pipeline: Pipeline, recorder: Recorder
) -> None:
    _register(recorder, ("normalize", ()))
    message_id = await pipeline.add_message()
    await _enqueue(message_id)
    await pipeline.drain()
    async with pipeline.database.sessionmaker() as session:
        await service.set_mailbox_enabled(session, pipeline.mailbox_id, False)
        await session.commit()

    await pipeline.execute("DELETE FROM mail_mailboxes WHERE id = :id", id=pipeline.mailbox_id)

    assert await _rows(pipeline, message_id) == {}
    settings = await pipeline.execute(
        "SELECT count(*) FROM processing_mailbox_settings WHERE mailbox_id = :id",
        id=pipeline.mailbox_id,
    )
    assert settings == [(0,)]


@pytest.fixture
def cli_database(pipeline: Pipeline, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("OLLAMAIL_DATABASE_URL", pipeline.settings.url.get_secret_value())
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _cli(*args: str) -> tuple[int, str]:
    # The CLI runs its own event loop.
    result = await asyncio.to_thread(CliRunner().invoke, cli, list(args))
    return result.exit_code, result.stdout + result.stderr


@pytest.mark.usefixtures("cli_database")
async def test_cli_reprocesses_a_step_in_a_time_range(
    pipeline: Pipeline, recorder: Recorder
) -> None:
    _register(recorder, ("normalize", ()), ("triage", ("normalize",)))
    old = await pipeline.add_message(received_at=datetime(2026, 1, 10, tzinfo=UTC))
    recent = await pipeline.add_message(received_at=datetime(2026, 9, 10, tzinfo=UTC))
    await _enqueue(old, recent)
    await pipeline.drain()
    recorder.calls.clear()

    code, output = await _cli(
        "processing",
        "reprocess",
        "--mailbox",
        str(pipeline.mailbox_id),
        "--since",
        "2026-09-01",
        "--step",
        "triage",
    )
    await pipeline.drain()

    assert code == 0, output
    assert "Queued 1 messages" in output
    assert recorder.calls == [("triage", recent)]
    jobs = await pipeline.execute(
        "SELECT DISTINCT priority FROM procrastinate_jobs WHERE id > ("
        " SELECT max(id) FROM procrastinate_jobs WHERE priority = :new)",
        new=int(Priority.NEW),
    )
    assert jobs == [(int(Priority.REPROCESS),)]


@pytest.mark.usefixtures("cli_database")
async def test_cli_switches_processing_per_mailbox(pipeline: Pipeline, recorder: Recorder) -> None:
    _register(recorder, ("normalize", ()))

    code, output = await _cli("processing", "disable", str(pipeline.mailbox_id))
    assert code == 0, output
    async with pipeline.database.sessionmaker() as session:
        assert not await service.is_mailbox_enabled(session, pipeline.mailbox_id)

    code, output = await _cli("processing", "enable", str(pipeline.mailbox_id))
    assert code == 0, output
    async with pipeline.database.sessionmaker() as session:
        assert await service.is_mailbox_enabled(session, pipeline.mailbox_id)

    code, output = await _cli("processing", "disable", str(uuid.uuid4()))
    assert code == 1
    assert "not found" in output
