"""``processing.requeue_outdated`` reads all messages only after a step changed (#187)."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from procrastinate import RetryStrategy
from sqlalchemy import select

from app.core.ids import uuid7_time
from app.processing import service, tasks
from app.processing.models import MessageProcessing, ProcessingScanState, StepStatus
from app.processing.steps import registry
from app.processing.tasks import Priority, enqueue_processing, requeue_outdated
from app.worker import app
from tests.processing.conftest import Pipeline, Recorder

pytestmark = pytest.mark.db


async def _process(*message_ids: uuid.UUID) -> None:
    async with app.open_async():
        for message_id in message_ids:
            await enqueue_processing(message_id, priority=Priority.NEW)


async def _requeue(pipeline: Pipeline) -> None:
    async with app.open_async():
        await requeue_outdated(timestamp=0)
    await pipeline.drain()


async def _state(pipeline: Pipeline) -> ProcessingScanState | None:
    async with pipeline.database.sessionmaker() as session:
        return await session.scalar(select(ProcessingScanState))


async def _planned(pipeline: Pipeline, message_id: uuid.UUID) -> bool:
    async with pipeline.database.sessionmaker() as session:
        found = await session.scalar(
            select(MessageProcessing.id).where(MessageProcessing.message_id == message_id)
        )
    return found is not None


async def test_only_new_messages_are_checked_until_a_step_changes(
    pipeline: Pipeline, recorder: Recorder
) -> None:
    registry.register(recorder.step("normalize"))
    planned = await pipeline.add_message()
    await _process(planned)
    await pipeline.drain()

    # First run: no state yet, so all messages are checked; nothing is outdated.
    await _requeue(pipeline)
    state = await _state(pipeline)
    assert state is not None and state.step_versions == {"normalize": 1}
    assert state.planned_before is not None

    # An unplanned message stored long before the last check is not looked for (this
    # is what makes the job cheap); one stored since (its job got lost) is found.
    old = await pipeline.add_message(stored_at=datetime.now(UTC) - timedelta(days=2))
    lost = await pipeline.add_message()
    recorder.calls.clear()
    await _requeue(pipeline)
    assert recorder.calls == [("normalize", lost)]
    assert not await _planned(pipeline, old)

    # A version bump checks all messages again: the old one is planned now, too.
    recorder.calls.clear()
    with registry.isolated(recorder.step("normalize", version=2)):
        await _requeue(pipeline)
        assert sorted(recorder.calls) == sorted(
            ("normalize", message_id) for message_id in (planned, old, lost)
        )
        await _requeue(pipeline)
    state = await _state(pipeline)
    assert state is not None and state.step_versions == {"normalize": 2}


async def test_enabling_a_mailbox_checks_all_messages_again(
    pipeline: Pipeline, recorder: Recorder
) -> None:
    registry.register(recorder.step("normalize"))
    async with pipeline.database.sessionmaker() as session:
        await service.set_mailbox_enabled(session, pipeline.mailbox_id, False)
        await session.commit()
    old = await pipeline.add_message(stored_at=datetime.now(UTC) - timedelta(days=2))
    await _requeue(pipeline)
    assert recorder.calls == []
    state = await _state(pipeline)
    assert state is not None and state.step_versions == {"normalize": 1}

    async with pipeline.database.sessionmaker() as session:
        await service.set_mailbox_enabled(session, pipeline.mailbox_id, True)
        await session.commit()
    state = await _state(pipeline)
    assert state is not None and state.step_versions is None
    await _requeue(pipeline)

    assert recorder.calls == [("normalize", old)]


async def test_unplanned_message_is_checked_until_it_is_planned(
    pipeline: Pipeline, recorder: Recorder
) -> None:
    steps = [registry.register(recorder.step("normalize"))]
    async with pipeline.database.sessionmaker() as session:
        assert await service.messages_to_requeue(session, steps, limit=10) == []
        await session.commit()
    # Stored just before the last check, but committed after it (or its job failed).
    stuck = await pipeline.add_message(stored_at=datetime.now(UTC) - timedelta(minutes=5))

    for _ in range(2):
        async with pipeline.database.sessionmaker() as session:
            assert await service.messages_to_requeue(session, steps, limit=10) == [stuck]
            await session.commit()
        state = await _state(pipeline)
        assert state is not None and state.planned_before is not None
        assert state.planned_before - service.PLANNED_MARGIN <= uuid7_time(stuck)

    await _process(stuck)
    await pipeline.drain()
    async with pipeline.database.sessionmaker() as session:
        assert await service.messages_to_requeue(session, steps, limit=10) == []
        await session.commit()


async def test_requeue_queues_in_batches(
    pipeline: Pipeline, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry.register(recorder.step("normalize"))
    monkeypatch.setattr(tasks, "REQUEUE_BATCH", 2)
    messages = [await pipeline.add_message() for _ in range(5)]
    # Already queued: its batch falls back to queueing one by one.
    await _process(messages[3])

    async with app.open_async():
        await tasks.requeue_messages(messages, Priority.REPROCESS)
    jobs = await pipeline.execute(
        "SELECT args->>'message_id', priority FROM procrastinate_jobs "
        "WHERE task_name = 'processing.plan_message' AND status = 'todo'"
    )

    assert sorted(jobs) == sorted(
        (str(message_id), int(Priority.NEW if message_id == messages[3] else Priority.REPROCESS))
        for message_id in messages
    )
    await pipeline.drain()
    assert sorted(recorder.calls) == sorted(("normalize", m) for m in messages)


async def test_check_of_all_messages_continues_below_the_previous_batch(
    pipeline: Pipeline, recorder: Recorder
) -> None:
    """After a version bump each run reads on from where the previous one stopped
    instead of from the newest message again (#226)."""
    registry.register(recorder.step("normalize"))
    newest_first = [await pipeline.add_message() for _ in range(5)][::-1]
    await _process(*newest_first)
    await pipeline.drain()
    await _requeue(pipeline)

    with registry.isolated(recorder.step("normalize", version=2)):
        steps = registry.ordered()

        async def run() -> list[uuid.UUID]:
            async with pipeline.database.sessionmaker() as session:
                found = await service.messages_to_requeue(session, steps, limit=2)
                await session.commit()
            return found

        # The plan jobs have not run yet, so the newer messages are still outdated; the
        # cursor skips them all the same.
        assert await run() == newest_first[0:2]
        assert await run() == newest_first[2:4]
        assert await run() == newest_first[4:]
        state = await _state(pipeline)
        assert state is not None and state.cursor is None
        assert state.step_versions == {"normalize": 1}

        # From the newest again: what the plan jobs did not bring up to date is found.
        await _process(*newest_first[1:])
        await pipeline.drain()
        assert await run() == newest_first[0:1]
        await _process(newest_first[0])
        await pipeline.drain()
        assert await run() == []
    state = await _state(pipeline)
    assert state is not None and state.step_versions == {"normalize": 2}
    assert state.cursor is None


async def test_message_whose_plan_job_gives_up_is_not_queued_again(
    pipeline: Pipeline, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A message that cannot be planned is marked failed instead of being queued every
    10 minutes and holding back ``planned_before`` (#226)."""
    steps = [registry.register(recorder.step("normalize"))]
    async with pipeline.database.sessionmaker() as session:
        assert await service.messages_to_requeue(session, steps, limit=10) == []
        await session.commit()
    broken = await pipeline.add_message()

    async def fail(*args: Any, **kwargs: Any) -> list[str]:
        raise RuntimeError("synthetic")

    monkeypatch.setattr(service, "plan", fail)
    monkeypatch.setattr(tasks.plan_message, "retry_strategy", RetryStrategy(max_attempts=1))
    await _requeue(pipeline)

    async with pipeline.database.sessionmaker() as session:
        rows = list(
            await session.scalars(
                select(MessageProcessing).where(MessageProcessing.message_id == broken)
            )
        )
        assert [(r.step, r.version, r.status, r.error_code) for r in rows] == [
            ("normalize", 1, StepStatus.FAILED, "runtime_error")
        ]
        assert await service.messages_to_requeue(session, steps, limit=10) == []
        await session.commit()
    state = await _state(pipeline)
    assert state is not None and state.planned_before is not None
    assert state.planned_before > uuid7_time(broken)
    assert recorder.calls == []

    # Reprocessing runs it again once planning works.
    monkeypatch.undo()
    async with pipeline.database.sessionmaker() as session:
        assert await service.reset_steps(session, [broken]) == 1
        await session.commit()
    await _process(broken)
    await pipeline.drain()
    assert recorder.calls == [("normalize", broken)]


async def test_plan_failure_is_only_recorded_when_no_retry_follows(
    pipeline: Pipeline, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry.register(recorder.step("normalize"))
    message = await pipeline.add_message()
    attempts = 0
    plan = service.plan

    async def fail_once(*args: Any, **kwargs: Any) -> list[str]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("synthetic")
        return await plan(*args, **kwargs)

    monkeypatch.setattr(service, "plan", fail_once)
    monkeypatch.setattr(tasks.plan_message, "retry_strategy", RetryStrategy(max_attempts=1))
    await _process(message)
    await pipeline.drain()

    assert attempts == 2
    assert recorder.calls == [("normalize", message)]


async def test_fail_plan_marks_missing_and_outdated_steps(
    pipeline: Pipeline, recorder: Recorder
) -> None:
    registry.register(recorder.step("normalize"))
    registry.register(recorder.step("extract"))
    message = await pipeline.add_message()
    await _process(message)
    await pipeline.drain()

    steps = [
        recorder.step("normalize", version=2),
        recorder.step("extract"),
        recorder.step("summary"),
    ]
    async with pipeline.database.sessionmaker() as session:
        assert await service.fail_plan(session, message, steps, "runtime_error")
        assert not await service.fail_plan(session, uuid.uuid4(), steps, "runtime_error")
        await session.commit()
        rows = await session.scalars(
            select(MessageProcessing).where(MessageProcessing.message_id == message)
        )
        found = {r.step: (r.version, r.status, r.error_code) for r in rows}
    assert found == {
        "normalize": (2, StepStatus.FAILED, "runtime_error"),
        "extract": (1, StepStatus.DONE, None),
        "summary": (1, StepStatus.FAILED, "runtime_error"),
    }
