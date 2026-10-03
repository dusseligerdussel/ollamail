"""Recovery from passing failures (#138): automatic retries of failed steps and the circuit
breaker for a dead LLM endpoint. Real worker and PostgreSQL, synthetic data."""

import uuid

import pytest
from sqlalchemy import select

from app.ai.llm.circuit import CircuitBreaker
from app.ai.llm.config import EnvConfigResolver
from app.ai.llm.errors import (
    LLMOutputError,
    LLMTimeoutError,
    LLMUnavailableError,
    ModelNotAvailableError,
)
from app.ai.llm.gateway import LLMGateway
from app.ai.llm.types import ChatMessage, LLMTask
from app.core.config import LLMSettings
from app.processing.models import MessageProcessing, StepStatus
from app.processing.service import StepCounts, count_steps_by_mailbox
from app.processing.steps import ProcessingStep, StepContext, registry
from app.processing.tasks import Priority, enqueue_processing, retry_failed
from app.worker import app
from tests.ai.fakes import FakeFactory, FakeProvider
from tests.processing.conftest import FAST_RETRY, Pipeline, Recorder, run_worker

pytestmark = pytest.mark.db


async def _enqueue(*message_ids: uuid.UUID) -> None:
    async with app.open_async():
        for message_id in message_ids:
            await enqueue_processing(message_id, priority=Priority.NEW)


async def _row(pipeline: Pipeline, message_id: uuid.UUID, step: str) -> MessageProcessing:
    async with pipeline.database.sessionmaker() as session:
        row = await session.scalar(
            select(MessageProcessing).where(
                MessageProcessing.message_id == message_id, MessageProcessing.step == step
            )
        )
    assert row is not None
    return row


async def _retry_due_now(pipeline: Pipeline) -> None:
    """Let the scheduled automatic retries come due and run ``processing.retry_failed``."""
    await pipeline.execute(
        "UPDATE message_processing SET retry_at = now() - interval '1 second' "
        "WHERE retry_at IS NOT NULL"
    )
    async with app.open_async():
        await retry_failed(timestamp=0)


async def test_passing_failure_is_retried_later(pipeline: Pipeline, recorder: Recorder) -> None:
    registry.register(recorder.step("triage", queue="llm", error=LLMUnavailableError("down")))
    recorder.failures["triage"] = 3
    message_id = await pipeline.add_message()

    await _enqueue(message_id)
    await pipeline.drain()

    row = await _row(pipeline, message_id, "triage")
    assert (row.status, row.error_code) == (StepStatus.FAILED, "llm_unavailable_error")
    assert row.retry_at is not None
    assert row.auto_retries == 0
    async with pipeline.database.sessionmaker() as session:
        counts = await count_steps_by_mailbox(session, [pipeline.mailbox_id])
    assert counts[pipeline.mailbox_id] == StepCounts(failed=1, retry_scheduled=1)

    # Not due yet: nothing happens.
    async with app.open_async():
        await retry_failed(timestamp=0)
    assert await pipeline.pending_jobs() == 0

    await _retry_due_now(pipeline)
    row = await _row(pipeline, message_id, "triage")
    assert (row.status, row.auto_retries) == (StepStatus.PENDING, 1)
    await pipeline.drain()

    assert recorder.names() == ["triage"] * 4
    row = await _row(pipeline, message_id, "triage")
    assert (row.status, row.error_code, row.retry_at, row.auto_retries) == (
        StepStatus.DONE,
        None,
        None,
        0,
    )


async def test_permanent_failure_is_not_retried(pipeline: Pipeline, recorder: Recorder) -> None:
    registry.register(recorder.step("triage", queue="llm", error=LLMOutputError("Triage", 2)))
    recorder.failures["triage"] = 10
    message_id = await pipeline.add_message()

    await _enqueue(message_id)
    await pipeline.drain()
    await _retry_due_now(pipeline)
    await pipeline.drain()

    assert recorder.names() == ["triage"] * 3
    row = await _row(pipeline, message_id, "triage")
    assert (row.status, row.error_code, row.retry_at) == (
        StepStatus.FAILED,
        "llm_output_error",
        None,
    )


async def test_timeout_is_retried_automatically_only_once(
    pipeline: Pipeline, recorder: Recorder
) -> None:
    registry.register(recorder.step("triage", queue="llm", error=LLMTimeoutError("slow")))
    recorder.failures["triage"] = 10
    message_id = await pipeline.add_message()

    await _enqueue(message_id)
    await pipeline.drain()
    assert (await _row(pipeline, message_id, "triage")).retry_at is not None

    await _retry_due_now(pipeline)
    await pipeline.drain()

    row = await _row(pipeline, message_id, "triage")
    assert (row.status, row.auto_retries, row.retry_at) == (StepStatus.FAILED, 1, None)
    # OLLAMAIL_PROCESSING_LLM_TIMEOUT_ATTEMPTS (2) runs per round, two rounds.
    assert recorder.names() == ["triage"] * 4


async def test_missing_model_fails_at_once_and_is_retried_later(
    pipeline: Pipeline, recorder: Recorder
) -> None:
    registry.register(recorder.step("triage", queue="llm", error=ModelNotAvailableError("m")))
    recorder.failures["triage"] = 1
    message_id = await pipeline.add_message()

    await _enqueue(message_id)
    await pipeline.drain()

    # No immediate retries: the model does not appear within minutes.
    assert recorder.names() == ["triage"]
    row = await _row(pipeline, message_id, "triage")
    assert (row.status, row.error_code) == (StepStatus.FAILED, "model_not_available_error")
    assert row.retry_at is not None

    # The admin pulled the model.
    await _retry_due_now(pipeline)
    await pipeline.drain()
    assert (await _row(pipeline, message_id, "triage")).status == StepStatus.DONE


class Clock:
    now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_llm_outage_does_not_cause_a_retry_storm(
    pipeline: Pipeline, recorder: Recorder
) -> None:
    clock = Clock()
    provider = FakeProvider(answers=[LLMUnavailableError("down")] * 100)
    gateway = LLMGateway(
        EnvConfigResolver(LLMSettings(default_chat_model="chat:1b")),
        provider_factory=FakeFactory(default=provider),
        circuit=CircuitBreaker(threshold=2, cooldown=60, clock=clock),
    )

    async def classify(ctx: StepContext) -> None:
        await gateway.complete(LLMTask.TRIAGE, [ChatMessage(role="user", content="test")])

    registry.register(
        ProcessingStep(name="triage", version=1, queue="llm", handler=classify, retry=FAST_RETRY)
    )
    message_ids = [await pipeline.add_message() for _ in range(10)]

    await _enqueue(*message_ids)
    async with app.open_async():
        for _ in range(3):
            await run_worker()

    # Two calls opened the breaker; every other attempt waits for the cooldown.
    assert len(provider.calls) == 2
    for message_id in message_ids:
        row = await _row(pipeline, message_id, "triage")
        assert row.status == StepStatus.PENDING
        assert row.attempts <= 2
    postponed = await pipeline.execute(
        "SELECT count(*) FROM procrastinate_jobs "
        "WHERE status = 'todo' AND task_name = 'processing.run_step' AND scheduled_at > now()"
    )
    assert postponed == [(10,)]

    # The endpoint is back after the cooldown.
    provider.answers = ["ok"] * 100
    clock.now += 600
    await pipeline.execute("UPDATE procrastinate_jobs SET scheduled_at = now()")
    await pipeline.drain()

    assert len(provider.calls) == 12
    for message_id in message_ids:
        assert (await _row(pipeline, message_id, "triage")).status == StepStatus.DONE
