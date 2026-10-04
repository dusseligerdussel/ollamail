"""Devices that receive notifications through Web Push (#181), and the push itself.

A device registers its browser subscription (``register_device``); the same browser is
stored once, for the user who registered it last. After the triage step announced a
message (``app.notifications.service.notify_triaged``), ``send_to_user`` runs in a worker
job per recipient and pushes only ``message_id`` and ``mailbox_id`` to each of the user's
devices. The service worker then fetches what it shows through
``GET /notifications/messages/{id}``, like an open tab does.

Functions take an open session and never commit.
"""

import hashlib
import json
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import NotificationsSettings
from app.core.logging import get_logger
from app.notifications.models import NotificationSettings, PushSubscription
from app.notifications.vapid import Vapid, VapidKeyError, check_subscription_keys
from app.notifications.webpush import PushOutcome, endpoint_allowed, push_service, send_push

log = get_logger(__name__)

# Devices per user; registering one more removes the one registered longest ago.
MAX_DEVICES_PER_USER = 20


class PushUnavailableError(Exception):
    """Web Push is switched off or not configured on this server."""


class InvalidSubscriptionError(Exception):
    """The endpoint is not on an allowed push service, or the keys are malformed."""


@dataclass(frozen=True, slots=True)
class Device:
    browser: str | None
    os: str | None
    mobile: bool


# Same rules as the session list in the frontend (src/lib/user-agent.ts). Order matters:
# Edge and Opera also contain "Chrome", Chrome also contains "Safari".
_BROWSERS = (
    (re.compile(r"Edg(e|A|iOS)?/"), "Edge"),
    (re.compile(r"OPR/|Opera"), "Opera"),
    (re.compile(r"Firefox/|FxiOS/"), "Firefox"),
    (re.compile(r"Chrome/|CriOS/"), "Chrome"),
    (re.compile(r"Safari/"), "Safari"),
)
_SYSTEMS = (
    (re.compile(r"Windows"), "Windows"),
    (re.compile(r"iPhone|iPad|iPod"), "iOS"),
    (re.compile(r"Android"), "Android"),
    (re.compile(r"Mac OS X|Macintosh"), "macOS"),
    (re.compile(r"CrOS"), "ChromeOS"),
    (re.compile(r"Linux"), "Linux"),
)
_MOBILE = re.compile(r"Mobi|iPhone|Android")


def describe_device(user_agent: str | None) -> Device:
    """Browser and system from the user agent; only these labels are stored."""
    if not user_agent:
        return Device(None, None, False)
    user_agent = user_agent[:512]
    browser = next((name for pattern, name in _BROWSERS if pattern.search(user_agent)), None)
    system = next((name for pattern, name in _SYSTEMS if pattern.search(user_agent)), None)
    return Device(browser, system, bool(_MOBILE.search(user_agent)))


def endpoint_hash(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()


def vapid_of(settings: NotificationsSettings) -> Vapid:
    if not settings.web_push_available or settings.vapid_private_key is None:
        raise PushUnavailableError
    return Vapid.from_keys(
        settings.vapid_public_key,
        settings.vapid_private_key.get_secret_value(),
        settings.vapid_subject,
    )


async def list_devices(session: AsyncSession, user_id: uuid.UUID) -> list[PushSubscription]:
    return list(
        await session.scalars(
            select(PushSubscription)
            .where(PushSubscription.user_id == user_id)
            .order_by(PushSubscription.created_at, PushSubscription.id)
        )
    )


async def register_device(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    endpoint: str,
    p256dh: str,
    auth: str,
    user_agent: str | None,
    settings: NotificationsSettings,
) -> PushSubscription:
    """Store the browser's subscription for ``user_id``, or update it (new keys, device).

    A browser subscribed by another user before now belongs to ``user_id``: one browser
    profile has one subscription, and it should only receive the notifications of the
    user signed in there."""
    if not settings.web_push_available:
        raise PushUnavailableError
    if not endpoint_allowed(endpoint, settings.web_push_allowed_hosts):
        raise InvalidSubscriptionError
    try:
        check_subscription_keys(p256dh, auth)
    except VapidKeyError:
        raise InvalidSubscriptionError from None

    digest = endpoint_hash(endpoint)
    device = describe_device(user_agent)
    stored = await session.scalar(
        select(PushSubscription).where(PushSubscription.endpoint_hash == digest).with_for_update()
    )
    if stored is not None and stored.user_id != user_id:
        await session.delete(stored)
        await session.flush()
        stored = None
    if stored is None:
        stored = PushSubscription(user_id=user_id, endpoint_hash=digest)
        session.add(stored)
    stored.subscription = {"endpoint": endpoint, "p256dh": p256dh, "auth": auth}
    stored.browser, stored.os, stored.mobile = device.browser, device.os, device.mobile
    await session.flush()

    devices = await list_devices(session, user_id)
    excess = [d.id for d in devices if d.id != stored.id][
        : max(0, len(devices) - MAX_DEVICES_PER_USER)
    ]
    if excess:
        await session.execute(delete(PushSubscription).where(PushSubscription.id.in_(excess)))
    await session.refresh(stored)
    return stored


async def remove_device(session: AsyncSession, user_id: uuid.UUID, device_id: uuid.UUID) -> bool:
    """Delete one of the user's devices; ``False`` if there is no such device of the user."""
    result = await session.execute(
        delete(PushSubscription)
        .where(PushSubscription.id == device_id, PushSubscription.user_id == user_id)
        .returning(PushSubscription.id)
    )
    return result.first() is not None


def push_payload(message_id: uuid.UUID, mailbox_id: uuid.UUID) -> bytes:
    """What a push carries: IDs only (docs/PRIVACY.md)."""
    return json.dumps(
        {
            "type": "notification.message",
            "message_id": str(message_id),
            "mailbox_id": str(mailbox_id),
        },
        separators=(",", ":"),
    ).encode("ascii")


class PushRetryError(Exception):
    """Some push services were not reachable or asked to wait; the job is retried."""


async def send_to_user(
    session: AsyncSession,
    http: httpx.AsyncClient,
    user_id: uuid.UUID,
    message_id: uuid.UUID,
    mailbox_id: uuid.UUID,
    settings: NotificationsSettings,
    *,
    only: Sequence[uuid.UUID] | None = None,
) -> list[uuid.UUID]:
    """Push the message to every device of the user (or only to ``only``).

    Subscriptions the push service reports as gone (404/410) are deleted. Returns the
    devices that should be tried again (rate limit, server error, no connection)."""
    if not settings.web_push_available:
        return []
    opted_in = await session.scalar(
        select(NotificationSettings.enabled).where(NotificationSettings.user_id == user_id)
    )
    if not opted_in:
        return []
    vapid = vapid_of(settings)
    query = select(PushSubscription).where(PushSubscription.user_id == user_id)
    if only is not None:
        query = query.where(PushSubscription.id.in_(only))
    devices = list(await session.scalars(query.order_by(PushSubscription.id)))
    payload = push_payload(message_id, mailbox_id)
    retry: list[uuid.UUID] = []
    for device in devices:
        result = await send_push(
            http,
            vapid,
            device.subscription,
            payload,
            ttl=settings.web_push_ttl_seconds,
            allowed_hosts=settings.web_push_allowed_hosts,
        )
        if result.outcome is PushOutcome.SENT:
            device.last_sent_at = datetime.now(UTC)
            continue
        # IDs, the push service's host and the status only: never the endpoint.
        log.info(
            "web_push_not_delivered",
            device_id=str(device.id),
            push_service=push_service(device.subscription["endpoint"]),
            outcome=result.outcome.value,
            status=result.status,
        )
        if result.outcome is PushOutcome.GONE:
            await session.delete(device)
        elif result.outcome is PushOutcome.RETRY:
            retry.append(device.id)
    await session.flush()
    return retry
