"""The triage step announces a new important mail to the owner who opted in (#149), also
through Web Push (#181, one job per mail since #185)."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import httpx
import pytest
import respx

from app.core.config import get_settings
from app.mail.models import Folder, FolderRole, message_folders
from app.notifications import tasks
from app.notifications.push import register_device
from app.notifications.service import save_settings
from app.processing.steps import registry
from app.processing.tasks import enqueue_processing
from app.triage.tasks import use_llm
from app.worker import app
from tests.factories import make_user
from tests.notifications.conftest import builtin_category
from tests.notifications.webpush import FCM, Browser, make_auth_session, push_settings
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
            await make_auth_session(session, pipeline.owner_id),
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
            "message_id": str(message_id),
            "mailbox_id": str(pipeline.mailbox_id),
            "user_ids": [str(pipeline.owner_id)],
        }
    ]


@respx.mock
async def test_one_job_per_mail_pushes_to_all_recipients_and_retries_only_failed_devices(
    pipeline: Pipeline,  # noqa: F811
    web_push: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tasks, "WEB_PUSH_RETRY_BASE_SECONDS", 0)
    browsers = {name: Browser(FCM + uuid.uuid4().hex) for name in ("owner", "busy", "reader")}
    routes = {
        name: respx.post(browser.endpoint).mock(return_value=httpx.Response(201))
        for name, browser in browsers.items()
    }
    routes["busy"].mock(side_effect=[httpx.Response(503), httpx.Response(201)])
    async with pipeline.database.sessionmaker() as session:
        reader = (await make_user(session)).id
        devices = {}
        owners = {"owner": pipeline.owner_id, "busy": pipeline.owner_id, "reader": reader}
        for name, user_id in owners.items():
            await save_settings(session, user_id, enabled=True)
            devices[name] = await register_device(
                session,
                user_id,
                await make_auth_session(session, user_id),
                endpoint=browsers[name].endpoint,
                p256dh=browsers[name].p256dh,
                auth=browsers[name].auth,
                user_agent=None,
                settings=get_settings().notifications,
            )
        await session.commit()
    message_id = uuid.uuid4()

    try:
        async with app.open_async():
            for _ in range(2):
                await tasks.enqueue_web_push(
                    [pipeline.owner_id, reader, pipeline.owner_id], message_id, pipeline.mailbox_id
                )
        await pipeline.drain()
    finally:
        # Committed users would show up in other tests (the fixture removes only the owner).
        await pipeline.execute("DELETE FROM users WHERE id = :id", id=reader)

    assert {name: route.call_count for name, route in routes.items()} == {
        "owner": 1,
        "busy": 2,
        "reader": 1,
    }
    jobs = await pipeline.execute(
        "SELECT queue_name, args FROM procrastinate_jobs"
        " WHERE task_name = 'notifications.web_push' ORDER BY id"
    )
    recipients = sorted([str(pipeline.owner_id), str(reader)])
    assert jobs == [
        (
            "push",
            {
                "message_id": str(message_id),
                "mailbox_id": str(pipeline.mailbox_id),
                "user_ids": recipients,
            },
        ),
        (
            "push",
            {
                "message_id": str(message_id),
                "mailbox_id": str(pipeline.mailbox_id),
                "user_ids": recipients,
                "device_ids": [str(devices["busy"].id)],
                "attempt": 2,
            },
        ),
    ]
