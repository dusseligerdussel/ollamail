"""Web Push devices and sending with PostgreSQL (#181). Push services are mocked (respx)."""

import json
import uuid

import httpx
import pytest
import respx
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import NotificationsSettings
from app.notifications import push, service
from app.notifications.models import PushSubscription
from app.users.models import User
from tests.factories import make_user
from tests.notifications.webpush import FCM, MOZILLA, Browser, push_settings

pytestmark = pytest.mark.db

FIREFOX = "Mozilla/5.0 (X11; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0"


@pytest.fixture
def push_config() -> NotificationsSettings:
    return push_settings()


async def _register(
    session: AsyncSession,
    user_id: uuid.UUID,
    browser: Browser,
    push_config: NotificationsSettings,
    user_agent: str | None = FIREFOX,
) -> PushSubscription:
    return await push.register_device(
        session,
        user_id,
        endpoint=browser.endpoint,
        p256dh=browser.p256dh,
        auth=browser.auth,
        user_agent=user_agent,
        settings=push_config,
    )


async def _opted_in(session: AsyncSession) -> uuid.UUID:
    user = await make_user(session)
    await service.save_settings(session, user.id, enabled=True)
    return user.id


async def test_a_browser_is_registered_once_with_encrypted_keys(
    db_session: AsyncSession, push_config: NotificationsSettings
) -> None:
    user = await make_user(db_session)
    browser = Browser(FCM + uuid.uuid4().hex)
    first = await _register(db_session, user.id, browser, push_config)
    assert (first.browser, first.os, first.mobile) == ("Firefox", "Linux", False)

    # The same browser again (e.g. with new keys): updated, not added.
    browser.auth_secret = b"fedcba9876543210"
    again = await _register(db_session, user.id, browser, push_config, user_agent=None)
    assert again.id == first.id
    assert (again.browser, again.subscription["auth"]) == (None, browser.auth)
    assert [d.id for d in await push.list_devices(db_session, user.id)] == [first.id]

    # Endpoint and keys are not stored in plain text.
    raw = await db_session.scalar(
        text("SELECT subscription FROM push_subscriptions WHERE id = :id"), {"id": first.id}
    )
    assert raw is not None and browser.endpoint not in raw and browser.p256dh not in raw


async def test_a_browser_belongs_to_the_user_who_registered_it_last(
    db_session: AsyncSession, push_config: NotificationsSettings
) -> None:
    erika, max_ = await make_user(db_session), await make_user(db_session)
    browser = Browser(FCM + uuid.uuid4().hex)
    await _register(db_session, erika.id, browser, push_config)
    device = await _register(db_session, max_.id, browser, push_config)
    assert await push.list_devices(db_session, erika.id) == []
    assert [d.id for d in await push.list_devices(db_session, max_.id)] == [device.id]


async def test_registering_more_devices_than_allowed_drops_the_oldest(
    db_session: AsyncSession, push_config: NotificationsSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(push, "MAX_DEVICES_PER_USER", 2)
    user = await make_user(db_session)
    devices = [
        await _register(db_session, user.id, Browser(FCM + uuid.uuid4().hex), push_config)
        for _ in range(3)
    ]
    remaining = [d.id for d in await push.list_devices(db_session, user.id)]
    assert sorted(remaining) == sorted(d.id for d in devices[1:])


@pytest.mark.parametrize(
    "endpoint",
    ["http://fcm.googleapis.com/fcm/send/x", "https://internal.example/x", "https://10.0.0.1/x"],
)
async def test_only_push_service_endpoints_are_accepted(
    db_session: AsyncSession, push_config: NotificationsSettings, endpoint: str
) -> None:
    user = await make_user(db_session)
    with pytest.raises(push.InvalidSubscriptionError):
        await _register(db_session, user.id, Browser(endpoint), push_config)


async def test_registering_needs_web_push(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    with pytest.raises(push.PushUnavailableError):
        await _register(db_session, user.id, Browser(FCM + "x"), NotificationsSettings())


async def test_users_remove_only_their_own_devices(
    db_session: AsyncSession, push_config: NotificationsSettings
) -> None:
    erika, max_ = await make_user(db_session), await make_user(db_session)
    device = await _register(db_session, erika.id, Browser(FCM + uuid.uuid4().hex), push_config)
    assert not await push.remove_device(db_session, max_.id, device.id)
    assert await push.remove_device(db_session, erika.id, device.id)
    assert not await push.remove_device(db_session, erika.id, device.id)


async def test_devices_are_deleted_with_the_user(
    db_session: AsyncSession, push_config: NotificationsSettings
) -> None:
    user = await make_user(db_session)
    await _register(db_session, user.id, Browser(FCM + uuid.uuid4().hex), push_config)
    await db_session.delete(await db_session.get(User, user.id))
    await db_session.flush()
    count = await db_session.scalar(
        select(func.count())
        .select_from(PushSubscription)
        .where(PushSubscription.user_id == user.id)
    )
    assert count == 0


@respx.mock
async def test_push_carries_ids_only_and_cleans_up_gone_devices(
    db_session: AsyncSession, push_config: NotificationsSettings
) -> None:
    user_id = await _opted_in(db_session)
    sent, gone, busy = (
        Browser(FCM + uuid.uuid4().hex),
        Browser(MOZILLA + uuid.uuid4().hex),
        Browser(FCM + uuid.uuid4().hex),
    )
    devices, routes = {}, {}
    for browser, status in ((sent, 201), (gone, 410), (busy, 503)):
        devices[browser.endpoint] = await _register(db_session, user_id, browser, push_config)
        routes[browser.endpoint] = respx.post(browser.endpoint).mock(
            return_value=httpx.Response(status)
        )
    message_id, mailbox_id = uuid.uuid4(), uuid.uuid4()

    async with httpx.AsyncClient() as http:
        retry = await push.send_to_user(
            db_session, http, user_id, message_id, mailbox_id, push_config
        )

    assert retry == [devices[busy.endpoint].id]
    payload = sent.payload(routes[sent.endpoint].calls.last.request.content)
    assert payload == {
        "type": "notification.message",
        "message_id": str(message_id),
        "mailbox_id": str(mailbox_id),
    }
    remaining = {d.id: d for d in await push.list_devices(db_session, user_id)}
    assert devices[gone.endpoint].id not in remaining
    assert remaining[devices[sent.endpoint].id].last_sent_at is not None
    assert remaining[devices[busy.endpoint].id].last_sent_at is None

    # A follow-up only reaches the devices that failed.
    async with httpx.AsyncClient() as http:
        await push.send_to_user(
            db_session, http, user_id, message_id, mailbox_id, push_config, only=retry
        )
    assert [route.call_count for route in routes.values()] == [1, 1, 2]


@respx.mock
async def test_nothing_is_pushed_without_opt_in_or_web_push(
    db_session: AsyncSession, push_config: NotificationsSettings
) -> None:
    user_id = await _opted_in(db_session)
    browser = Browser(FCM + uuid.uuid4().hex)
    await _register(db_session, user_id, browser, push_config)
    route = respx.post(browser.endpoint).mock(return_value=httpx.Response(201))
    async with httpx.AsyncClient() as http:
        await push.send_to_user(
            db_session, http, user_id, uuid.uuid4(), uuid.uuid4(), NotificationsSettings()
        )
        await service.save_settings(db_session, user_id, enabled=False)
        await push.send_to_user(db_session, http, user_id, uuid.uuid4(), uuid.uuid4(), push_config)
    assert not route.called


def test_payload_holds_ids_only() -> None:
    message_id, mailbox_id = uuid.uuid4(), uuid.uuid4()
    assert json.loads(push.push_payload(message_id, mailbox_id)) == {
        "type": "notification.message",
        "message_id": str(message_id),
        "mailbox_id": str(mailbox_id),
    }
