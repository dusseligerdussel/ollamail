"""Notification settings of a user and the record of mails already announced.

Both tables only hold IDs and switches, no mail content. Deleting the user removes the
settings, deleting the message removes its record (``ON DELETE CASCADE``).
"""

import uuid

from sqlalchemy import ForeignKey, false
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

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
