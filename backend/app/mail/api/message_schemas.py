"""Response schemas of the read API for mails (inbox list, thread, attachments).

HTML is always sanitised server-side (``app.mail.sanitize``); the raw ``body_html`` never
leaves the server.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.mail.actions import MessageAction
from app.mail.models import MailboxType


class AddressRead(BaseModel):
    name: str | None = None
    address: str


class MessageSummary(BaseModel):
    """One row of the inbox list."""

    id: uuid.UUID
    mailbox_id: uuid.UUID
    thread_id: uuid.UUID | None
    subject: str
    sender: AddressRead | None
    # Start of the text without quotes and signature.
    snippet: str
    # Received, else sent, else stored.
    date: datetime
    unread: bool
    flagged: bool
    has_attachments: bool


class MessagePage(BaseModel):
    items: list[MessageSummary]
    # Pass as ``cursor`` for the next page; ``null`` on the last page.
    next_cursor: str | None
    # Number of messages matching the filter (all pages); only on the first page
    # (without ``cursor``), ``null`` on the following ones.
    total: int | None


class AttachmentRead(BaseModel):
    id: uuid.UUID
    filename: str | None
    content_type: str
    size: int
    is_inline: bool


class MessageBody(BaseModel):
    """Sanitised HTML of a message. External images are removed unless requested."""

    # ``null`` for plain-text mails; show ``text`` instead.
    html: str | None
    # External images removed from ``html``; the UI offers to load them if > 0.
    blocked_images: int


class MessageDetail(MessageSummary):
    to: list[AddressRead]
    cc: list[AddressRead]
    reply_to: list[AddressRead]
    sent_at: datetime | None
    # Full plain text (fallback without HTML).
    text: str
    body: MessageBody
    attachments: list[AttachmentRead]


class ThreadRead(BaseModel):
    """A conversation, oldest message first. A message without thread is its own thread."""

    thread_id: uuid.UUID | None
    mailbox_id: uuid.UUID
    subject: str
    messages: list[MessageDetail]


class MessageUpdate(BaseModel):
    """Fields left out stay as they are. Both are written back to the mail server."""

    # ``false`` marks the message unread.
    seen: bool | None = None
    # Flag (IMAP ``\\Flagged``, Graph flag, Gmail star).
    flagged: bool | None = None


class MessageActionRequest(BaseModel):
    """``archive``, ``trash`` (to the trash folder, never deleted for good) or ``move``
    into ``folder_id`` (a folder or label of the message's mailbox)."""

    action: MessageAction
    folder_id: uuid.UUID | None = None


class MessageActionResult(BaseModel):
    message: MessageSummary
    # Folders/labels of the message now.
    folder_ids: list[uuid.UUID]
    # Move here to undo (``action: move``): the inbox if the message was there, else the
    # folder it came from. ``null`` if there is nothing to go back to.
    undo_folder_id: uuid.UUID | None


ConnectMethod = Literal["credentials", "oauth"]


class MailboxProviderRead(BaseModel):
    """A mailbox type that can be added on this instance."""

    type: MailboxType
    # ``credentials``: form with connection test (``POST /mailboxes``). ``oauth``: start
    # the flow with ``POST <oauth_start_path>`` (body ``{"return_to": "<path>"}``) and
    # navigate to the returned ``authorization_url``.
    connect: ConnectMethod
    oauth_start_path: str | None = None
