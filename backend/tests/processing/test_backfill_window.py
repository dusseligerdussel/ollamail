"""Backfill window (#141): mails older than ``OLLAMAIL_PROCESSING_BACKFILL_LLM_DAYS`` skip
the ``recent_only`` steps (triage, todos) unless their mailbox opted in. Synthetic data."""

import asyncio
import dataclasses
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from app.cli import cli
from app.core.config import ProcessingSettings, get_settings
from app.processing import service, tasks
from app.processing.models import MessageProcessing, StepStatus
from app.processing.steps import ProcessingStep, registry
from app.processing.tasks import Priority, enqueue_processing, recent_since, requeue_messages
from app.worker import app
from tests.processing.conftest import Pipeline, Recorder

pytestmark = pytest.mark.db

OLD = datetime.now(UTC) - timedelta(days=60)
RECENT = datetime.now(UTC) - timedelta(days=2)


def _recent_only(step: ProcessingStep) -> ProcessingStep:
    return dataclasses.replace(step, recent_only=True)


@pytest.fixture
def steps(recorder: Recorder) -> Recorder:
    """``index`` for every mail; ``triage`` and ``todos`` for recent mail only;
    ``write_back`` depends on ``triage``."""
    registry.register(recorder.step("index"))
    registry.register(_recent_only(recorder.step("triage", queue="llm")))
    registry.register(recorder.step("write_back", depends_on=("triage",)))
    registry.register(
        _recent_only(dataclasses.replace(recorder.step("todos", queue="llm"), after=("triage",)))
    )
    return recorder


async def _process(*message_ids: uuid.UUID, priority: Priority = Priority.BACKFILL) -> None:
    async with app.open_async():
        for message_id in message_ids:
            await enqueue_processing(message_id, priority=priority)


async def _statuses(pipeline: Pipeline, message_id: uuid.UUID) -> dict[str, StepStatus]:
    async with pipeline.database.sessionmaker() as session:
        rows = await session.scalars(
            select(MessageProcessing).where(MessageProcessing.message_id == message_id)
        )
        return {row.step: row.status for row in rows}


def test_window_start_follows_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 10, 3, tzinfo=UTC)
    settings = get_settings().model_copy(
        update={"processing": ProcessingSettings(backfill_llm_days=14)}
    )
    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    assert recent_since(now) == now - timedelta(days=14)

    settings.processing.backfill_llm_days = 0
    assert recent_since(now) is None


def test_backfill_window_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    assert ProcessingSettings().backfill_llm_days == 14
    monkeypatch.setenv("OLLAMAIL_PROCESSING_BACKFILL_LLM_DAYS", "30")
    assert ProcessingSettings().backfill_llm_days == 30


async def test_old_mail_gets_the_index_only(pipeline: Pipeline, steps: Recorder) -> None:
    old = await pipeline.add_message(received_at=OLD)
    recent = await pipeline.add_message(received_at=RECENT)

    await _process(old, recent)
    await pipeline.drain()

    assert steps.names(old) == ["index"]
    assert await _statuses(pipeline, old) == {
        "index": StepStatus.DONE,
        "triage": StepStatus.SKIPPED,
        # Depends on a skipped step.
        "write_back": StepStatus.SKIPPED,
        "todos": StepStatus.SKIPPED,
    }
    assert sorted(steps.names(recent)) == ["index", "todos", "triage", "write_back"]
    assert set((await _statuses(pipeline, recent)).values()) == {StepStatus.DONE}


async def test_no_window_classifies_every_mail(
    pipeline: Pipeline, steps: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tasks, "recent_since", lambda: None)
    old = await pipeline.add_message(received_at=OLD)

    await _process(old)
    await pipeline.drain()

    assert sorted(steps.names(old)) == ["index", "todos", "triage", "write_back"]


async def test_skipped_steps_are_counted_and_not_requeued(
    pipeline: Pipeline, steps: Recorder
) -> None:
    old = [await pipeline.add_message(received_at=OLD) for _ in range(2)]
    await _process(*old)
    await pipeline.drain()

    async with pipeline.database.sessionmaker() as session:
        counts = await service.count_steps_by_mailbox(session, [pipeline.mailbox_id])
        outdated = await service.outdated_messages(session, registry.ordered(), limit=10)
    # Messages, not steps; skipped steps are neither pending nor failed.
    assert counts[pipeline.mailbox_id] == service.StepCounts(skipped_messages=2)
    assert outdated == []


async def test_already_classified_old_mail_is_kept(pipeline: Pipeline, steps: Recorder) -> None:
    """Triage that ran before (e.g. before an update) is not thrown away."""
    old = await pipeline.add_message(received_at=OLD)
    async with pipeline.database.sessionmaker() as session:
        await service.plan(session, old, registry.ordered())
        await service.reset_steps(session, [old])
        await session.commit()
    await pipeline.execute(
        "UPDATE message_processing SET status = 'done' WHERE message_id = :id AND step = 'triage'",
        id=old,
    )

    await _process(old)
    await pipeline.drain()

    statuses = await _statuses(pipeline, old)
    assert statuses["triage"] == StepStatus.DONE
    # Its dependent runs; only the unclassified todos are skipped.
    assert statuses["write_back"] == StepStatus.DONE
    assert statuses["todos"] == StepStatus.SKIPPED


async def test_include_older_classifies_skipped_mail(pipeline: Pipeline, steps: Recorder) -> None:
    older = await pipeline.add_message(received_at=OLD)
    oldest = await pipeline.add_message(received_at=OLD - timedelta(days=30))
    await _process(older, oldest)
    await pipeline.drain()
    steps.calls.clear()

    async with pipeline.database.sessionmaker() as session:
        message_ids = await service.include_older(session, pipeline.mailbox_id)
        await session.commit()
    # Most recently received first.
    assert message_ids == [older, oldest]
    async with app.open_async():
        await requeue_messages(message_ids, Priority.REPROCESS)
    await pipeline.drain()

    assert sorted(steps.names(older)) == ["todos", "triage", "write_back"]
    assert set((await _statuses(pipeline, oldest)).values()) == {StepStatus.DONE}

    # From now on, older mails of this mailbox are classified at once.
    steps.calls.clear()
    imported = await pipeline.add_message(received_at=OLD)
    await _process(imported)
    await pipeline.drain()
    assert sorted(steps.names(imported)) == ["index", "todos", "triage", "write_back"]


async def test_include_older_skips_disabled_mailboxes(pipeline: Pipeline, steps: Recorder) -> None:
    old = await pipeline.add_message(received_at=OLD)
    await _process(old)
    await pipeline.drain()

    async with pipeline.database.sessionmaker() as session:
        await service.set_mailbox_enabled(session, pipeline.mailbox_id, False)
        assert await service.include_older(session, pipeline.mailbox_id) == []
        await session.commit()
    assert (await _statuses(pipeline, old))["triage"] == StepStatus.SKIPPED


async def _cli(*args: str) -> tuple[int, str]:
    # The CLI runs its own event loop.
    result = await asyncio.to_thread(CliRunner().invoke, cli, list(args))
    return result.exit_code, result.stdout + result.stderr


@pytest.fixture
def cli_database(pipeline: Pipeline, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("OLLAMAIL_DATABASE_URL", pipeline.settings.url.get_secret_value())
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_cli_include_older(pipeline: Pipeline, steps: Recorder, cli_database: None) -> None:
    old = await pipeline.add_message(received_at=OLD)
    await _process(old)
    await pipeline.drain()

    code, output = await _cli("processing", "include-older", str(pipeline.mailbox_id))
    assert code == 0, output
    assert "Queued 1 older messages" in output
    await pipeline.drain()
    assert set((await _statuses(pipeline, old)).values()) == {StepStatus.DONE}

    code, _ = await _cli("processing", "include-older", str(uuid.uuid4()))
    assert code == 1
