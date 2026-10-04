import uuid
from datetime import datetime

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


class PushDeviceRead(BaseModel):
    """A device that receives notifications through Web Push."""

    id: uuid.UUID
    # Browser and system as recognised from the user agent, ``null`` if unknown.
    browser: str | None
    os: str | None
    mobile: bool
    # Host of the browser vendor's push service, e.g. ``fcm.googleapis.com``.
    push_service: str
    created_at: datetime
    last_sent_at: datetime | None


class WebPushRead(BaseModel):
    # ``false`` unless the admin switched Web Push on and configured the VAPID keys.
    available: bool
    # VAPID public key for ``PushManager.subscribe`` (``applicationServerKey``).
    public_key: str | None
    devices: list[PushDeviceRead]


class PushSubscriptionKeys(BaseModel):
    p256dh: str = Field(min_length=1, max_length=200)
    auth: str = Field(min_length=1, max_length=100)


class PushSubscriptionCreate(BaseModel):
    """The browser's ``PushSubscription.toJSON()`` (``expirationTime`` is ignored)."""

    endpoint: str = Field(min_length=1, max_length=2048)
    keys: PushSubscriptionKeys
