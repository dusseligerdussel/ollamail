"""The triage step announces a new important mail to the owner who opted in (#149), also
through Web Push (#181)."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import httpx
import pytest
import respx

from app.core.config import get_settings
from app.mail.models import Folder, FolderRole, message_folders
from app.notifications.push import register_device
from app.notifications.service import save_settings
from app.processing.steps import registry
from app.processing.tasks import enqueue_processing
from app.triage.tasks import use_llm
from app.worker import app
from tests.notifications.conftest import builtin_category
from tests.notifications.webpush import FCM, Browser, push_settings
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


@pytest.fixture
def web_push(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Web Push switched on for the worker (it reads the environment)."""
    config = push_settings()
    assert config.vapid_private_key is not None
    for name, value in {
        "WEB_PUSH_ENABLED": "true",
        "VAPID_PUBLIC_KEY": config.vapid_public_key,
        "VAPID_PRIVATE_KEY": config.vapid_private_key.get_secret_value(),
        "VAPID_SUBJECT": config.vapid_subject,
    }.items():
        monkeypatch.setenv(f"OLLAMAIL_NOTIFICATIONS_{name}", value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@respx.mock
async def test_an_announced_mail_is_pushed_to_the_devices_of_the_user(
    pipeline: Pipeline,  # noqa: F811
    triage_step: None,
    fake_llm: FakeLLM,  # noqa: F811
    web_push: None,
) -> None:
    browser = Browser(FCM + uuid.uuid4().hex)
    route = respx.post(browser.endpoint).mock(return_value=httpx.Response(201))
    async with pipeline.database.sessionmaker() as session:
        await save_settings(
            session,
            pipeline.owner_id,
            enabled=True,
            category_ids=[await builtin_category(session, "important")],
        )
        await register_device(
            session,
            pipeline.owner_id,
            endpoint=browser.endpoint,
            p256dh=browser.p256dh,
            auth=browser.auth,
            user_agent=None,
            settings=get_settings().notifications,
        )
        await session.commit()
    message_id = await _inbox_message(pipeline)
    fake_llm.answer(answer("important", 1))

    with use_llm(fake_llm.gateway):
        async with app.open_async():
            await enqueue_processing(message_id)
        await pipeline.drain()

    assert route.call_count == 1
    assert browser.payload(route.calls.last.request.content) == {
        "type": "notification.message",
        "message_id": str(message_id),
        "mailbox_id": str(pipeline.mailbox_id),
    }
    # The job got IDs only.
    jobs = await pipeline.execute(
        "SELECT args FROM procrastinate_jobs WHERE task_name = 'notifications.web_push'"
    )
    assert [args for (args,) in jobs] == [
        {
            "user_id": str(pipeline.owner_id),
            "message_id": str(message_id),
            "mailbox_id": str(pipeline.mailbox_id),
        }
    ]
