"""Who is told about a new message, and what a notification may show.

``notify_triaged`` runs in the triage step, right after a new message got its category.
It publishes the event ``notification.message`` (IDs only, docs/PRIVACY.md) to every reader
of the mailbox who opted in to the message's category. The browser then fetches the few
fields it shows through ``GET /notifications/messages/{id}``, which honours the user's
choice of showing the subject.

A message is announced at most once, and only if it is new: received within
``OLLAMAIL_NOTIFICATIONS_MAX_AGE_MINUTES``, unread, in the inbox, and classified by the
pipeline (not corrected by a user). Functions take an open session and never commit.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import exists, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from app.core.config import NotificationsSettings
from app.core.events import Event, publish
from app.core.ids import uuid7
from app.mail.access import readers, visible_to
from app.mail.models import Folder, FolderRole, Mailbox, Message, message_folders
from app.mail.providers.base import Flag
from app.notifications.models import MailNotification, NotificationSettings
from app.triage.categories import effective_categories
from app.triage.models import TriageCategory, TriageResult, TriageSource

NOTIFICATION_EVENT = "notification.message"


class UnknownCategoryError(Exception):
    """A category ID that is not in the user's list of categories."""


async def user_settings(session: AsyncSession, user_id: uuid.UUID) -> NotificationSettings:
    """The user's settings (defaults if none are stored; not added to the session)."""
    stored = await session.scalar(
        select(NotificationSettings).where(NotificationSettings.user_id == user_id)
    )
    return stored or NotificationSettings(
        user_id=user_id, enabled=False, category_ids=[], show_subject=False, sound=False
    )


async def save_settings(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    enabled: bool | None = None,
    category_ids: Sequence[uuid.UUID] | None = None,
    show_subject: bool | None = None,
    sound: bool | None = None,
) -> NotificationSettings:
    """Update the given fields; ``category_ids`` must be categories of the user."""
    if category_ids is not None:
        known = {c.id for c in await effective_categories(session, user_id, include_hidden=True)}
        if any(category_id not in known for category_id in category_ids):
            raise UnknownCategoryError
    stored = await session.scalar(
        select(NotificationSettings)
        .where(NotificationSettings.user_id == user_id)
        .with_for_update()
    )
    if stored is None:
        stored = NotificationSettings(
            user_id=user_id, enabled=False, category_ids=[], show_subject=False, sound=False
        )
        session.add(stored)
    if enabled is not None:
        stored.enabled = enabled
    if category_ids is not None:
        # Unique, in the given order.
        stored.category_ids = list(dict.fromkeys(category_ids))
    if show_subject is not None:
        stored.show_subject = show_subject
    if sound is not None:
        stored.sound = sound
    await session.flush()
    return stored


async def notify_triaged(
    session: AsyncSession,
    message_id: uuid.UUID,
    mailbox_id: uuid.UUID,
    settings: NotificationsSettings,
    *,
    now: datetime | None = None,
) -> list[uuid.UUID]:
    """Announce a newly triaged message to the readers who opted in to its category.

    Returns the users notified (empty if the message does not qualify or was announced
    before). The event is sent when the surrounding transaction commits."""
    if not settings.enabled:
        return []
    since = (now or datetime.now(UTC)) - timedelta(minutes=settings.max_age_minutes)
    in_inbox = exists(
        select(message_folders.c.message_id)
        .join(Folder, Folder.id == message_folders.c.folder_id)
        .where(message_folders.c.message_id == Message.id, Folder.role == FolderRole.INBOX)
    )
    category_id = await session.scalar(
        select(TriageResult.category_id)
        .join(Message, Message.id == TriageResult.message_id)
        .join(Mailbox, Mailbox.id == Message.mailbox_id)
        .where(
            TriageResult.message_id == message_id,
            Message.mailbox_id == mailbox_id,
            TriageResult.source != TriageSource.USER,
            TriageResult.category_id.is_not(None),
            ~Message.flags.contains([Flag.SEEN.value]),
            Message.sort_date >= since,
            Mailbox.deletion_requested_at.is_(None),
            in_inbox,
        )
    )
    if category_id is None:
        return []
    recipients = list(
        await session.scalars(
            select(NotificationSettings.user_id)
            .where(
                NotificationSettings.user_id.in_(readers(mailbox_id)),
                NotificationSettings.enabled,
                NotificationSettings.category_ids.contains([category_id]),
            )
            .order_by(NotificationSettings.user_id)
        )
    )
    if not recipients:
        return []
    inserted = await session.scalar(
        insert(MailNotification)
        .values(id=uuid7(), message_id=message_id)
        .on_conflict_do_nothing(index_elements=[MailNotification.message_id])
        .returning(MailNotification.id)
    )
    if inserted is None:
        return []
    event = Event(type=NOTIFICATION_EVENT, ids={"message_id": message_id, "mailbox_id": mailbox_id})
    for user_id in recipients:
        await publish(session, user_id, event)
    return recipients


@dataclass(frozen=True, slots=True)
class NotificationContent:
    message_id: uuid.UUID
    mailbox_id: uuid.UUID
    sender: str | None
    category_id: uuid.UUID | None
    category_name: str | None
    category_builtin_key: str | None
    subject: str | None
    sound: bool


async def notification_content(
    session: AsyncSession, user_id: uuid.UUID, message_id: uuid.UUID
) -> NotificationContent | None:
    """What the notification of ``message_id`` shows to ``user_id``: sender and category,
    the subject only if the user chose so. ``None`` if the user may not read the message."""
    row = (
        await session.execute(
            select(Message, TriageCategory)
            .join(Mailbox, Mailbox.id == Message.mailbox_id)
            .outerjoin(TriageResult, TriageResult.message_id == Message.id)
            .outerjoin(TriageCategory, TriageCategory.id == TriageResult.category_id)
            .where(Message.id == message_id, visible_to(user_id))
            # Not the bodies and headers: a notification shows none of them.
            .options(load_only(Message.id, Message.mailbox_id, Message.sender, Message.subject))
        )
    ).first()
    if row is None:
        return None
    message, category = row
    stored = await user_settings(session, user_id)
    sender = message.sender or {}
    return NotificationContent(
        message_id=message.id,
        mailbox_id=message.mailbox_id,
        sender=sender.get("name") or sender.get("address") or None,
        category_id=category.id if category is not None else None,
        category_name=category.name if category is not None else None,
        category_builtin_key=category.builtin_key if category is not None else None,
        subject=message.subject if stored.show_subject else None,
        sound=stored.sound,
    )
