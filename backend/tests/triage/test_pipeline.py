"""The triage steps in the processing pipeline, with a real worker and a fake LLM."""

from collections.abc import Iterator

import pytest
from sqlalchemy import select

from app.mail.models import MailboxType
from app.mail.providers.base import RemoteFolder
from app.mail.providers.fake import FakeMailProvider
from app.mail.providers.registry import registry as provider_registry
from app.processing.models import MessageProcessing, StepStatus
from app.processing.steps import registry
from app.processing.tasks import enqueue_processing
from app.triage.models import TriageResult, TriageSource, WriteBackMode
from app.triage.tasks import TRIAGE_STEP_VERSION, use_llm
from app.triage.writeback import set_write_back_mode
from app.worker import app
from tests.processing.conftest import Pipeline, pipeline  # noqa: F401
from tests.processing.test_pipeline import notifications  # noqa: F401
from tests.triage.conftest import FakeLLM, answer

RAW = b"From: a@example.org\r\nSubject: Test\r\n\r\nHello\r\n"


def test_steps_are_registered() -> None:
    triage = registry.get("triage")
    write_back = registry.get("triage_write_back")

    assert triage is not None and write_back is not None
    assert (triage.queue, triage.version, triage.depends_on) == ("llm", TRIAGE_STEP_VERSION, ())
    assert (write_back.queue, write_back.depends_on) == ("sync", ("triage",))


@pytest.fixture
def triage_steps() -> Iterator[None]:
    triage, write_back = registry.get("triage"), registry.get("triage_write_back")
    assert triage is not None and write_back is not None
    with registry.isolated(triage, write_back):
        yield


@pytest.fixture
def server() -> Iterator[FakeMailProvider]:
    provider = FakeMailProvider()
    provider.add_folder(RemoteFolder("INBOX", "INBOX"))
    saved = provider_registry._factories.get(MailboxType.IMAP)
    provider_registry.register(MailboxType.IMAP, lambda config: provider, replace=True)
    yield provider
    if saved is None:
        provider_registry.unregister(MailboxType.IMAP)
    else:
        provider_registry.register(MailboxType.IMAP, saved, replace=True)


@pytest.mark.db
async def test_new_message_is_triaged_and_labelled(
    pipeline: Pipeline,  # noqa: F811
    triage_steps: None,
    fake_llm: FakeLLM,
    server: FakeMailProvider,
    notifications: list[dict[str, object]],  # noqa: F811
) -> None:
    message_id = await pipeline.add_message()
    remote_ref = server.add_message("INBOX", RAW)
    async with pipeline.database.sessionmaker() as session:
        await pipeline.execute(
            "UPDATE mail_messages SET remote_ref = :ref WHERE id = :id",
            ref=remote_ref,
            id=message_id,
        )
        await set_write_back_mode(session, pipeline.mailbox_id, WriteBackMode.LABEL)
        await session.commit()
    fake_llm.answer(answer("action_required", 1, "Needs a reply."))

    with use_llm(fake_llm.gateway):
        async with app.open_async():
            await enqueue_processing(message_id)
        await pipeline.drain()

    async with pipeline.database.sessionmaker() as session:
        result = await session.scalar(
            select(TriageResult).where(TriageResult.message_id == message_id)
        )
        steps = {
            row.step: row.status
            for row in await session.scalars(
                select(MessageProcessing).where(MessageProcessing.message_id == message_id)
            )
        }
    assert result is not None
    assert (result.source, result.priority, result.reason) == (
        TriageSource.LLM,
        1,
        "Needs a reply.",
    )
    assert steps == {"triage": StepStatus.DONE, "triage_write_back": StepStatus.DONE}
    assert (result.remote_label, result.write_back_pending) == (
        "ollamail/action_required",
        False,
    )
    assert "ollamail/action_required" in server.messages[remote_ref].flags
    # The UI hears about the category as soon as it is known.
    assert {
        "user_id": str(pipeline.owner_id),
        "event": {
            "type": "message.triaged",
            "ids": {"message_id": str(message_id), "mailbox_id": str(pipeline.mailbox_id)},
            "status": None,
        },
    } in notifications
