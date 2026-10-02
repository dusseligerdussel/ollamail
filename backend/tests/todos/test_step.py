"""The registered ``todos`` step, run by a real worker through the pipeline."""

import uuid

import pytest
from sqlalchemy import select

from app.core.ids import uuid7
from app.mail.models import Message
from app.processing.models import MessageProcessing, StepStatus
from app.processing.steps import registry
from app.processing.tasks import enqueue_processing
from app.todos.models import Todo
from app.todos.steps import STEP, use_llm
from app.worker import TASK_MODULES, app
from tests.processing.conftest import Pipeline
from tests.todos.conftest import WEDNESDAY_MORNING, FakeLLM

pytestmark = pytest.mark.db


def test_step_is_registered_after_triage() -> None:
    assert "app.todos.steps" in TASK_MODULES
    step = registry.get(STEP)
    assert step is not None
    assert (step.queue, step.depends_on, step.after) == ("llm", (), ("triage",))


async def _add_message(pipeline: Pipeline, body: str) -> uuid.UUID:
    message_id = uuid7()
    async with pipeline.database.sessionmaker() as session:
        session.add(
            Message(
                id=message_id,
                mailbox_id=pipeline.mailbox_id,
                remote_ref=str(message_id),
                sender={"name": "Max", "address": "max@example.com"},
                sent_at=WEDNESDAY_MORNING,
                body_text=body,
                body_main=body,
                language="en",
            )
        )
        await session.commit()
    return message_id


async def test_pipeline_runs_todo_extraction(pipeline: Pipeline, fake_llm: FakeLLM) -> None:
    step = registry.get(STEP)
    assert step is not None
    fake_llm.answer([{"title": "Send report", "due_phrase": "by Friday", "confidence": 0.9}])
    message_id = await _add_message(pipeline, "Please send me the report by Friday.")

    with registry.isolated(step), use_llm(fake_llm.gateway):
        async with app.open_async():
            await enqueue_processing(message_id)
        await pipeline.drain()

    async with pipeline.database.sessionmaker() as session:
        status = await session.scalar(
            select(MessageProcessing.status).where(MessageProcessing.message_id == message_id)
        )
        todos = list(await session.scalars(select(Todo).where(Todo.message_id == message_id)))
    assert status == StepStatus.DONE
    assert [
        (t.title, t.user_id, t.due_date.isoformat() if t.due_date else None) for t in todos
    ] == [("Send report", pipeline.owner_id, "2026-10-09")]
