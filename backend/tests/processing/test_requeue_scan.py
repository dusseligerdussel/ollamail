"""``processing.requeue_outdated`` reads all messages only after a step changed (#187)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.ids import uuid7_time
from app.processing import service, tasks
from app.processing.models import MessageProcessing, ProcessingScanState
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
