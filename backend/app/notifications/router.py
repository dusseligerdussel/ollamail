"""Notification API: the user's opt-in, the devices for Web Push and the content of one
notification.

``GET /notifications/messages/{id}`` answers only for messages the user may read
(``app.mail.access``), 404 otherwise, so IDs of others are not confirmed.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentSessionDep, SettingsDep
from app.core.config import Settings
from app.core.db import get_db
from app.core.errors import ProblemError
from app.notifications import push, service
from app.notifications.models import NotificationSettings, PushSubscription
from app.notifications.schemas import (
    MailNotificationRead,
    NotificationCategory,
    NotificationSettingsRead,
    NotificationSettingsUpdate,
    PushDeviceRead,
    PushSubscriptionCreate,
    WebPushRead,
)
from app.notifications.webpush import push_service

router = APIRouter(
    prefix="/notifications",
    tags=["notifications"],
    responses={401: {"description": "Not signed in"}},
)

DbDep = Annotated[AsyncSession, Depends(get_db)]
NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"description": "No such message"}}


def _settings_read(stored: NotificationSettings, settings: Settings) -> NotificationSettingsRead:
    available = settings.notifications.enabled
    return NotificationSettingsRead(
        available=available,
        enabled=available and bool(stored.enabled),
        category_ids=list(stored.category_ids or []),
        show_subject=bool(stored.show_subject),
        sound=bool(stored.sound),
    )


@router.get("/settings")
async def get_notification_settings(
    current: CurrentSessionDep, db: DbDep, settings: SettingsDep
) -> NotificationSettingsRead:
    """Own notification settings. Off until the user opts in."""
    stored = await service.user_settings(db, current.user_id)
    return _settings_read(stored, settings)


@router.put(
    "/settings",
    responses={422: {"description": "Unknown category (`unknown_category`)"}},
)
async def update_notification_settings(
    body: NotificationSettingsUpdate,
    current: CurrentSessionDep,
    db: DbDep,
    settings: SettingsDep,
) -> NotificationSettingsRead:
    try:
        stored = await service.save_settings(
            db,
            current.user_id,
            enabled=body.enabled,
            category_ids=body.category_ids,
            show_subject=body.show_subject,
            sound=body.sound,
        )
    except service.UnknownCategoryError:
        raise ProblemError(422, detail="Unknown category.", error_code="unknown_category") from None
    await db.commit()
    return _settings_read(stored, settings)


@router.get("/messages/{message_id}", responses=NOT_FOUND)
async def get_mail_notification(
    message_id: uuid.UUID, current: CurrentSessionDep, db: DbDep
) -> MailNotificationRead:
    """Content of the notification about a message: sender and category, the subject only
    if the user turned it on."""
    content = await service.notification_content(db, current.user_id, message_id)
    if content is None:
        raise ProblemError(404, detail="Message not found.")
    category = (
        NotificationCategory(
            id=content.category_id,
            name=content.category_name or "",
            builtin_key=content.category_builtin_key,
        )
        if content.category_id is not None
        else None
    )
    return MailNotificationRead(
        message_id=content.message_id,
        mailbox_id=content.mailbox_id,
        sender=content.sender,
        category=category,
        subject=content.subject,
        sound=content.sound,
    )


def _device_read(device: PushSubscription) -> PushDeviceRead:
    return PushDeviceRead(
        id=device.id,
        browser=device.browser,
        os=device.os,
        mobile=device.mobile,
        push_service=push_service(device.subscription["endpoint"]),
        created_at=device.created_at,
        last_sent_at=device.last_sent_at,
    )


@router.get("/push")
async def get_web_push(current: CurrentSessionDep, db: DbDep, settings: SettingsDep) -> WebPushRead:
    """Whether Web Push is available, the key browsers subscribe with, and the user's devices
    (listed also while Web Push is off, so they can be removed)."""
    config = settings.notifications
    available = config.web_push_available
    devices = await push.list_devices(db, current.user_id)
    return WebPushRead(
        available=available,
        public_key=config.vapid_public_key if available else None,
        devices=[_device_read(device) for device in devices],
    )


@router.post(
    "/push/devices",
    status_code=status.HTTP_201_CREATED,
    responses={
        409: {"description": "Web Push is off on this server (`web_push_unavailable`)"},
        422: {"description": "Push service not allowed or malformed keys (`invalid_subscription`)"},
    },
)
async def register_push_device(
    body: PushSubscriptionCreate,
    request: Request,
    current: CurrentSessionDep,
    db: DbDep,
    settings: SettingsDep,
) -> PushDeviceRead:
    """Register this browser for Web Push, or refresh its registration (same endpoint)."""
    try:
        device = await push.register_device(
            db,
            current.user_id,
            endpoint=body.endpoint,
            p256dh=body.keys.p256dh,
            auth=body.keys.auth,
            user_agent=request.headers.get("user-agent"),
            settings=settings.notifications,
        )
    except push.PushUnavailableError:
        raise ProblemError(
            409, detail="Web Push is not available.", error_code="web_push_unavailable"
        ) from None
    except push.InvalidSubscriptionError:
        raise ProblemError(
            422, detail="Invalid push subscription.", error_code="invalid_subscription"
        ) from None
    await db.commit()
    return _device_read(device)


@router.delete(
    "/push/devices/{device_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={404: {"description": "No such device"}},
)
async def remove_push_device(
    device_id: uuid.UUID, current: CurrentSessionDep, db: DbDep
) -> Response:
    """Stop Web Push to one of the user's devices."""
    if not await push.remove_device(db, current.user_id, device_id):
        raise ProblemError(404, detail="Device not found.")
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
