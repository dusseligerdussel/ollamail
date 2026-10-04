import uuid

from pydantic import BaseModel, Field


class NotificationSettingsRead(BaseModel):
    # ``false`` if the admin switched notifications off (``OLLAMAIL_NOTIFICATIONS_ENABLED``).
    available: bool
    enabled: bool
    category_ids: list[uuid.UUID]
    show_subject: bool
    sound: bool


class NotificationSettingsUpdate(BaseModel):
    """Fields left out stay unchanged."""

    enabled: bool | None = None
    category_ids: list[uuid.UUID] | None = Field(default=None, max_length=100)
    show_subject: bool | None = None
    sound: bool | None = None


class NotificationCategory(BaseModel):
    id: uuid.UUID
    name: str
    # Set for built-in categories; the UI shows a translated name for these.
    builtin_key: str | None


class MailNotificationRead(BaseModel):
    """What a notification about a message shows."""

    message_id: uuid.UUID
    mailbox_id: uuid.UUID
    # Display name of the sender, else the address.
    sender: str | None
    category: NotificationCategory | None
    # Only if the user chose to see subjects in notifications, ``null`` otherwise.
    subject: str | None
    # Let the browser play its notification sound.
    sound: bool
