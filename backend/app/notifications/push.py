"""Devices that receive notifications through Web Push (#181), and the push itself.

A device registers its browser subscription (``register_device``); the same browser is
stored once, for the user and sign-in session that registered it last, and is deleted with
that session (#185). After the triage step announced a message
(``app.notifications.service.notify_triaged``), one worker job per message pushes only
``message_id`` and ``mailbox_id`` to the devices of its recipients: ``load_targets`` reads
them in a short session, ``deliver`` sends without a database session (in parallel), and
``record_results`` stores the outcome. The service worker then fetches what it shows through
``GET /notifications/messages/{id}``, like an open tab does.

Functions take an open session and never commit.
"""

import asyncio
import hashlib
import json
import re
import uuid
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import AuthSession
from app.core.config import AuthSettings, NotificationsSettings
from app.core.logging import get_logger
from app.notifications.models import NotificationSettings, PushSubscription
from app.notifications.vapid import Vapid, VapidKeyError, check_subscription_keys
from app.notifications.webpush import (
    PushOutcome,
    PushServiceBreaker,
    endpoint_allowed,
    push_service,
    send_push,
)

log = get_logger(__name__)

# Devices per user; registering one more removes the one registered longest ago.
MAX_DEVICES_PER_USER = 20
# Devices one job sends to at the same time.
PARALLEL_SENDS = 10


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
    session_id: uuid.UUID,
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
    user signed in there. It is bound to ``session_id`` (the session it is registered
    from); signing out or losing that session deletes it."""
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
    stored.session_id = session_id
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


@dataclass(frozen=True, slots=True)
class PushTarget:
    """A device to push to, read before sending (no database session while sending)."""

    id: uuid.UUID
    subscription: dict[str, str]


@dataclass(slots=True)
class PushResults:
    sent: list[uuid.UUID] = field(default_factory=list)
    # The push service reported the subscription as gone (404/410): delete it.
    gone: list[uuid.UUID] = field(default_factory=list)
    # Rate limit, server error, no connection or service skipped: try again later.
    retry: list[uuid.UUID] = field(default_factory=list)


async def load_targets(
    session: AsyncSession,
    user_ids: Collection[uuid.UUID],
    auth: AuthSettings,
    *,
    only: Collection[uuid.UUID] | None = None,
) -> list[PushTarget]:
    """Devices of the users who still opted in (or only ``only`` of them), whose session is
    still valid: an expired or idle session that housekeeping has not deleted yet gets
    nothing either."""
    if not user_ids:
        return []
    now = datetime.now(UTC)
    query = (
        select(PushSubscription)
        .join(NotificationSettings, NotificationSettings.user_id == PushSubscription.user_id)
        .join(AuthSession, AuthSession.id == PushSubscription.session_id)
        .where(
            PushSubscription.user_id.in_(user_ids),
            NotificationSettings.enabled,
            AuthSession.expires_at > now,
            AuthSession.last_seen_at > now - timedelta(minutes=auth.session_idle_timeout_minutes),
        )
        .order_by(PushSubscription.id)
    )
    if only is not None:
        query = query.where(PushSubscription.id.in_(only))
    return [
        PushTarget(device.id, dict(device.subscription)) for device in await session.scalars(query)
    ]


async def deliver(
    http: httpx.AsyncClient,
    vapid: Vapid,
    targets: Collection[PushTarget],
    message_id: uuid.UUID,
    mailbox_id: uuid.UUID,
    settings: NotificationsSettings,
    *,
    breaker: PushServiceBreaker | None = None,
) -> PushResults:
    """Push the message to the devices, ``PARALLEL_SENDS`` at a time. Needs no session."""
    payload = push_payload(message_id, mailbox_id)
    limit = asyncio.Semaphore(PARALLEL_SENDS)
    results = PushResults()

    async def one(target: PushTarget) -> None:
        async with limit:
            result = await send_push(
                http,
                vapid,
                target.subscription,
                payload,
                ttl=settings.web_push_ttl_seconds,
                allowed_hosts=settings.web_push_allowed_hosts,
                breaker=breaker,
            )
        if result.outcome is PushOutcome.SENT:
            results.sent.append(target.id)
            return
        # IDs, the push service's host and the status only: never the endpoint.
        log.info(
            "web_push_not_delivered",
            device_id=str(target.id),
            push_service=push_service(target.subscription["endpoint"]),
            outcome=result.outcome.value,
            status=result.status,
        )
        if result.outcome is PushOutcome.GONE:
            results.gone.append(target.id)
        elif result.outcome is PushOutcome.RETRY:
            results.retry.append(target.id)

    await asyncio.gather(*(one(target) for target in targets))
    return results


async def record_results(session: AsyncSession, results: PushResults) -> None:
    """Note when devices last got a message and delete the gone ones."""
    if results.sent:
        await session.execute(
            update(PushSubscription)
            .where(PushSubscription.id.in_(results.sent))
            .values(last_sent_at=datetime.now(UTC))
        )
    if results.gone:
        await session.execute(delete(PushSubscription).where(PushSubscription.id.in_(results.gone)))
