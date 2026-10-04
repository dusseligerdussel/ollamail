"""The triage step announces a new important mail to the owner who opted in (#149)."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from app.mail.models import Folder, FolderRole, message_folders
from app.notifications.service import save_settings
from app.processing.steps import registry
from app.processing.tasks import enqueue_processing
from app.triage.tasks import use_llm
from app.worker import app
from tests.notifications.conftest import builtin_category
from tests.processing.conftest import Pipeline, pipeline  # noqa: F401
from tests.processing.test_pipeline import notifications  # noqa: F401
from tests.triage.conftest import FakeLLM, answer, fake_llm  # noqa: F401

pytestmark = pytest.mark.db


@pytest.fixture
def triage_step() -> Iterator[None]:
    triage = registry.get("triage")
    assert triage is not None
    with registry.isolated(triage):
        yield


async def _inbox_message(pipeline: Pipeline) -> uuid.UUID:  # noqa: F811
    message_id = await pipeline.add_message(received_at=datetime.now(UTC))
    async with pipeline.database.sessionmaker() as session:
        inbox = Folder(
            mailbox_id=pipeline.mailbox_id, remote_id="INBOX", name="INBOX", role=FolderRole.INBOX
        )
        session.add(inbox)
        await session.flush()
        await session.execute(
            message_folders.insert().values(message_id=message_id, folder_id=inbox.id)
        )
        await session.commit()
    return message_id


@pytest.mark.parametrize(("category", "notified"), [("action_required", True), ("info", False)])
async def test_new_mail_in_an_opted_in_category_is_announced(
    pipeline: Pipeline,  # noqa: F811
    triage_step: None,
    fake_llm: FakeLLM,  # noqa: F811
    notifications: list[dict[str, object]],  # noqa: F811
    category: str,
    notified: bool,
) -> None:
    async with pipeline.database.sessionmaker() as session:
        await save_settings(
            session,
            pipeline.owner_id,
            enabled=True,
            category_ids=[await builtin_category(session, "action_required")],
        )
        await session.commit()
    message_id = await _inbox_message(pipeline)
    fake_llm.answer(answer(category, 1))

    with use_llm(fake_llm.gateway):
        async with app.open_async():
            await enqueue_processing(message_id)
        await pipeline.drain()

    event = {
        "user_id": str(pipeline.owner_id),
        "event": {
            "type": "notification.message",
            "ids": {"message_id": str(message_id), "mailbox_id": str(pipeline.mailbox_id)},
            "status": None,
        },
    }
    assert (event in notifications) is notified
