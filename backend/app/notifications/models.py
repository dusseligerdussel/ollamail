"""Notification settings of a user, the record of mails already announced and the devices
that receive Web Push.

The tables hold IDs, switches and device data, no mail content. Deleting the user removes the
settings, deleting the message removes its record (``ON DELETE CASCADE``).
"""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String, false
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.crypto import EncryptedJSON
from app.core.db import Base


class NotificationSettings(Base):
    """Opt-in of one user. Without a row, notifications are off."""

    __tablename__ = "notification_settings"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )
    enabled: Mapped[bool] = mapped_column(server_default=false(), default=False)
    # Triage categories that trigger a notification (IDs from the user's effective list).
    category_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), default=list, server_default="{}"
    )
    # Show the subject in the notification (off: sender and category only).
    show_subject: Mapped[bool] = mapped_column(server_default=false(), default=False)
    # Let the browser play its notification sound (off: silent).
    sound: Mapped[bool] = mapped_column(server_default=false(), default=False)


class MailNotification(Base):
    """A message that was announced; a message is announced at most once (re-triage,
    retries and reprocessing do not notify again)."""

    __tablename__ = "mail_notifications"

    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("mail_messages.id", ondelete="CASCADE"), unique=True
    )


class PushSubscription(Base):
    """A device that receives notifications through Web Push (#181).

    ``subscription`` holds the push endpoint and the browser's keys, encrypted: the endpoint
    is a capability URL (whoever knows it and the keys can send to the device).
    ``endpoint_hash`` (SHA-256) finds an endpoint again without decrypting, so the same
    browser is registered once. Browser and system come from the user agent and only label
    the device in the list. Bound to the sign-in session it was registered in (#185): deleted
    with it (sign-out, revoked session, expiry, ``ON DELETE CASCADE``), with the user or by
    the user.
    """

    __tablename__ = "push_subscriptions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("auth_sessions.id", ondelete="CASCADE"), index=True
    )
    endpoint_hash: Mapped[str] = mapped_column(String(64), unique=True)
    # {"endpoint": ..., "p256dh": ..., "auth": ...}
    subscription: Mapped[dict[str, str]] = mapped_column(EncryptedJSON)
    browser: Mapped[str | None] = mapped_column(String(32))
    os: Mapped[str | None] = mapped_column(String(32))
    mobile: Mapped[bool] = mapped_column(server_default=false(), default=False)
    # Last message the push service accepted for this device.
    last_sent_at: Mapped[datetime | None]
