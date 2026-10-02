"""Optional predecessors (``ProcessingStep.after``): ordering if registered, ignored if
not, and a failed predecessor does not block (todos run without a triage result)."""

import uuid

import pytest
from sqlalchemy import select

from app.processing.models import MessageProcessing, StepStatus
from app.processing.steps import ProcessingStep, StepContext, StepError, StepRegistry, registry
from app.processing.tasks import enqueue_processing
from app.worker import app
from tests.processing.conftest import Pipeline, Recorder


async def _noop(ctx: StepContext) -> None:
    return None


def _step(
    name: str, *, after: tuple[str, ...] = (), depends_on: tuple[str, ...] = ()
) -> ProcessingStep:
    return ProcessingStep(
        name=name, version=1, queue="default", handler=_noop, depends_on=depends_on, after=after
    )


def test_ordered_puts_registered_after_steps_first() -> None:
    steps = StepRegistry()
    steps.register(_step("todos", after=("triage", "summary")))
    steps.register(_step("triage"))

    assert [s.name for s in steps.ordered()] == ["triage", "todos"]


def test_unregistered_after_steps_are_ignored() -> None:
    steps = StepRegistry()
    steps.register(_step("todos", after=("triage",)))

    assert [s.name for s in steps.ordered()] == ["todos"]


def test_cycles_through_after_are_rejected() -> None:
    steps = StepRegistry()
    steps.register(_step("a", after=("b",)))
    steps.register(_step("b", depends_on=("a",)))

    with pytest.raises(ValueError, match="cycle"):
        steps.ordered()


def test_step_decorator_accepts_after() -> None:
    steps = StepRegistry()

    @steps.step("todos", version=1, queue="llm", after=("triage",))
    async def extract(ctx: StepContext) -> None:
        return None

    step = steps.get("todos")
    assert step is not None
    assert step.after == ("triage",)


async def _run(pipeline: Pipeline) -> uuid.UUID:
    message_id = await pipeline.add_message()
    async with app.open_async():
        await enqueue_processing(message_id)
    await pipeline.drain()
    return message_id


async def _statuses(pipeline: Pipeline, message_id: uuid.UUID) -> dict[str, StepStatus]:
    async with pipeline.database.sessionmaker() as session:
        rows = await session.scalars(
            select(MessageProcessing).where(MessageProcessing.message_id == message_id)
        )
        return {row.step: row.status for row in rows}


@pytest.mark.db
async def test_runs_without_the_after_step(pipeline: Pipeline, recorder: Recorder) -> None:
    step = recorder.step("todos", queue="llm")
    registry.register(ProcessingStep(**{**step.__dict__, "after": ("triage",)}))

    message_id = await _run(pipeline)

    assert recorder.names() == ["todos"]
    assert await _statuses(pipeline, message_id) == {"todos": StepStatus.DONE}


@pytest.mark.db
async def test_waits_for_a_registered_after_step(pipeline: Pipeline, recorder: Recorder) -> None:
    todos = recorder.step("todos", queue="llm")
    registry.register(ProcessingStep(**{**todos.__dict__, "after": ("triage",)}))
    registry.register(recorder.step("triage", queue="llm"))

    await _run(pipeline)

    assert recorder.names() == ["triage", "todos"]


@pytest.mark.db
async def test_failed_after_step_does_not_block(pipeline: Pipeline, recorder: Recorder) -> None:
    todos = recorder.step("todos", queue="llm")
    registry.register(ProcessingStep(**{**todos.__dict__, "after": ("triage",)}))
    registry.register(recorder.step("triage", error=StepError("model_missing", permanent=True)))
    registry.register(recorder.step("summary", depends_on=("triage",)))
    recorder.failures["triage"] = 1

    message_id = await _run(pipeline)

    assert recorder.names() == ["triage", "todos"]
    assert await _statuses(pipeline, message_id) == {
        "triage": StepStatus.FAILED,
        "todos": StepStatus.DONE,
        # A hard dependency still blocks.
        "summary": StepStatus.PENDING,
    }


@pytest.mark.db
async def test_after_step_failing_after_retries_does_not_block(
    pipeline: Pipeline, recorder: Recorder
) -> None:
    todos = recorder.step("todos", queue="llm")
    registry.register(ProcessingStep(**{**todos.__dict__, "after": ("triage",)}))
    registry.register(recorder.step("triage"))
    # FAST_RETRY: the first attempt plus two retries.
    recorder.failures["triage"] = 3

    message_id = await _run(pipeline)

    assert recorder.names() == ["triage", "triage", "triage", "todos"]
    assert (await _statuses(pipeline, message_id))["todos"] == StepStatus.DONE
