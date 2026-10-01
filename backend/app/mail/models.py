"""Mail data model: mailboxes, folders, threads, messages, attachments and sync state.

Everything hangs off ``Mailbox`` with ``ON DELETE CASCADE`` foreign keys, so deleting a
mailbox removes all of its data in the database (docs/PRIVACY.md, Löschkonzept).
Attachment files live outside the database; ``app.mail.service.delete_mailbox`` removes
them together with the rows.

Ownership: a mailbox belongs to exactly one user (``owner_user_id``, deleted together with
the user) or is shared (``owner_user_id IS NULL``). The assignment table for shared
mailboxes (mailbox ↔ user/group) follows in #34.
"""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Enum,
    ForeignKey,
    Index,
    String,
    Table,
    Text,
    UniqueConstraint,
    false,
    true,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.crypto import EncryptedJSON
from app.core.db import Base


class MailboxType(enum.StrEnum):
    IMAP = "imap"
    GRAPH = "graph"
    GMAIL = "gmail"


class FolderKind(enum.StrEnum):
    # A real folder: a message lives in exactly one (IMAP, Microsoft Graph).
    FOLDER = "folder"
    # A label: a message can carry several (Gmail).
    LABEL = "label"


class FolderRole(enum.StrEnum):
    """Special-use roles (RFC 6154 for IMAP, well-known folders in Graph, system labels
    in Gmail)."""

    INBOX = "inbox"
    SENT = "sent"
    DRAFTS = "drafts"
    TRASH = "trash"
    JUNK = "junk"
    ARCHIVE = "archive"
    ALL = "all"


def _str_enum(enum_class: type[enum.StrEnum], name: str) -> Enum:
    # VARCHAR + CHECK instead of a native Postgres enum: new values need no type migration.
    return Enum(
        enum_class,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=16,
        values_callable=lambda members: [member.value for member in members],
    )


class Mailbox(Base):
    __tablename__ = "mail_mailboxes"
    __table_args__ = (
        CheckConstraint(
            "(is_shared AND owner_user_id IS NULL)"
            " OR (NOT is_shared AND owner_user_id IS NOT NULL)",
            name="owner",
        ),
    )

    type: Mapped[MailboxType] = mapped_column(_str_enum(MailboxType, "mailbox_type"))
    display_name: Mapped[str] = mapped_column(String(255))
    address: Mapped[str] = mapped_column(String(320))
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    is_shared: Mapped[bool] = mapped_column(server_default=false())
    # Non-secret connection settings (host, port, TLS, tenant ID, ...).
    provider_settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default="{}"
    )
    # Passwords and OAuth tokens, envelope-encrypted (docs/PRIVACY.md).
    credentials: Mapped[dict[str, Any] | None] = mapped_column(EncryptedJSON)
    sync_enabled: Mapped[bool] = mapped_column(server_default=true())
    # See ``app.mail.schemas.SyncSettings``.
    sync_settings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")

    folders: Mapped[list["Folder"]] = relationship(
        back_populates="mailbox", cascade="all, delete-orphan", passive_deletes=True
    )


class Folder(Base):
    """A folder (IMAP, Graph) or label (Gmail) on the server."""

    __tablename__ = "mail_folders"
    __table_args__ = (UniqueConstraint("mailbox_id", "remote_id"),)

    mailbox_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("mail_mailboxes.id", ondelete="CASCADE")
    )
    # Provider identifier: IMAP mailbox name, Graph folder ID, Gmail label ID.
    remote_id: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    kind: Mapped[FolderKind] = mapped_column(
        _str_enum(FolderKind, "folder_kind"), default=FolderKind.FOLDER
    )
    role: Mapped[FolderRole | None] = mapped_column(_str_enum(FolderRole, "folder_role"))
    sync_enabled: Mapped[bool] = mapped_column(server_default=true())

    mailbox: Mapped[Mailbox] = relationship(back_populates="folders")


class Thread(Base):
    __tablename__ = "mail_threads"
    __table_args__ = (
        # Explicit names: the naming convention only uses the first column.
        Index("ix_mail_threads_mailbox_id_provider_thread_id", "mailbox_id", "provider_thread_id"),
        Index("ix_mail_threads_mailbox_id_subject_key", "mailbox_id", "subject_key"),
    )

    mailbox_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("mail_mailboxes.id", ondelete="CASCADE")
    )
    # Server-side conversation ID (Gmail threadId, Graph conversationId), if any.
    provider_thread_id: Mapped[str | None] = mapped_column(Text)
    # Normalised subject for the threading fallback, see ``app.mail.threads``.
    subject_key: Mapped[str] = mapped_column(Text, default="")
    last_message_at: Mapped[datetime | None]


message_folders = Table(
    "mail_message_folders",
    Base.metadata,
    Column(
        "message_id",
        ForeignKey("mail_messages.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "folder_id",
        ForeignKey("mail_folders.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    ),
)


class Message(Base):
    __tablename__ = "mail_messages"
    __table_args__ = (
        UniqueConstraint("mailbox_id", "remote_ref"),
        Index("ix_mail_messages_mailbox_id_message_id_header", "mailbox_id", "message_id_header"),
        Index(None, "thread_id"),
    )

    mailbox_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("mail_mailboxes.id", ondelete="CASCADE")
    )
    thread_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("mail_threads.id", ondelete="SET NULL")
    )
    # Provider reference used for actions on the server (see ``RawMessage.remote_ref``).
    remote_ref: Mapped[str] = mapped_column(Text)

    message_id_header: Mapped[str | None] = mapped_column(Text)
    in_reply_to: Mapped[str | None] = mapped_column(Text)
    references: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    subject: Mapped[str] = mapped_column(Text, default="")
    # Addresses as ``{"name": str | None, "address": str}`` objects.
    sender: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    to: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, server_default="[]")
    cc: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, server_default="[]")
    bcc: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, server_default="[]")
    reply_to: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, server_default="[]")
    # All decoded header fields as ``[name, value]`` pairs, in message order.
    headers: Mapped[list[list[str]]] = mapped_column(JSONB, default=list, server_default="[]")
    sent_at: Mapped[datetime | None]
    received_at: Mapped[datetime | None]

    # Full plain text (from text/plain or converted from HTML).
    body_text: Mapped[str] = mapped_column(Text, default="")
    # Original HTML; sanitise with ``app.mail.sanitize.sanitize_html`` before display.
    body_html: Mapped[str | None] = mapped_column(Text)
    # ``body_text`` without quoted replies and signature.
    body_main: Mapped[str] = mapped_column(Text, default="")
    signature: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(8))
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    # Normalised flags (``app.mail.providers.base.Flag``) plus server keywords.
    flags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    has_attachments: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())

    thread: Mapped[Thread | None] = relationship()
    folders: Mapped[list[Folder]] = relationship(secondary=message_folders)
    attachments: Mapped[list["Attachment"]] = relationship(
        back_populates="message", cascade="all, delete-orphan", passive_deletes=True
    )


class Attachment(Base):
    """Attachment metadata; the content is a file below the data directory."""

    __tablename__ = "mail_attachments"

    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("mail_messages.id", ondelete="CASCADE"), index=True
    )
    # Denormalised so files can be deleted per mailbox without joins.
    mailbox_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("mail_mailboxes.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str | None] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(String(255))
    size: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    # ``Content-ID`` without angle brackets, for ``cid:`` references in HTML.
    content_id: Mapped[str | None] = mapped_column(Text)
    is_inline: Mapped[bool] = mapped_column(server_default=false())
    # Path relative to the data directory; never derived from the filename.
    storage_path: Mapped[str] = mapped_column(Text)

    message: Mapped[Message] = relationship(back_populates="attachments")


class SyncState(Base):
    """Sync cursor of one folder, or of the whole mailbox (``folder_id IS NULL``) for
    providers with a mailbox-wide change log (Gmail ``historyId``)."""

    __tablename__ = "mail_sync_states"
    __table_args__ = (
        UniqueConstraint("mailbox_id", "folder_id", postgresql_nulls_not_distinct=True),
    )

    mailbox_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("mail_mailboxes.id", ondelete="CASCADE")
    )
    folder_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("mail_folders.id", ondelete="CASCADE")
    )
    # Provider-specific cursor, see ``app.mail.providers.base.SyncCursor``.
    cursor: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    last_synced_at: Mapped[datetime | None]
    # Machine-readable error code of the last failed sync; never server messages.
    last_error: Mapped[str | None] = mapped_column(String(64))
