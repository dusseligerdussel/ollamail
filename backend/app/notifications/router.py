"""Notification API: the user's opt-in and the content of one notification.

``GET /notifications/messages/{id}`` answers only for messages the user may read
(``app.mail.access``), 404 otherwise, so IDs of others are not confirmed.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentSessionDep, SettingsDep
from app.core.config import Settings
from app.core.db import get_db
from app.core.errors import ProblemError
from app.notifications import service
from app.notifications.models import NotificationSettings
from app.notifications.schemas import (
    MailNotificationRead,
    NotificationCategory,
    NotificationSettingsRead,
    NotificationSettingsUpdate,
)

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
