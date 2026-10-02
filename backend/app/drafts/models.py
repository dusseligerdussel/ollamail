"""Reply drafts and the per-user drafting settings.

A draft belongs to one user and one mailbox (``ON DELETE CASCADE`` on both): deleting the
user or the mailbox deletes it. The answered mail and its thread are only referenced
(``SET NULL``): a draft whose mail was deleted stays visible to its author but can no
longer be sent. Who may see a draft is decided with ``app.mail.access`` on every request:
its author, while they can still read the mailbox.
"""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Enum, ForeignKey, Index, String, Text, false, true
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class DraftStatus(enum.StrEnum):
    DRAFT = "draft"
    SENT = "sent"
    DISCARDED = "discarded"


class ReplyDraft(Base):
    __tablename__ = "reply_drafts"
    __table_args__ = (Index("ix_reply_drafts_user_id_status", "user_id", "status"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    mailbox_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("mail_mailboxes.id", ondelete="CASCADE"), index=True
    )
    thread_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("mail_threads.id", ondelete="SET NULL")
    )
    # The answered mail.
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("mail_messages.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[DraftStatus] = mapped_column(
        Enum(
            DraftStatus,
            name="draft_status",
            native_enum=False,
            create_constraint=True,
            length=16,
            values_callable=lambda members: [member.value for member in members],
        ),
        default=DraftStatus.DRAFT,
    )
    reply_all: Mapped[bool] = mapped_column(server_default=false())
    # Addresses as ``{"name": str | None, "address": str}`` objects.
    to: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, server_default="[]")
    cc: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, server_default="[]")
    subject: Mapped[str] = mapped_column(Text, default="")
    body: Mapped[str] = mapped_column(Text, default="")
    # Append the answered mail as quoted text when sending.
    quote_original: Mapped[bool] = mapped_column(server_default=true())
    # The user's instruction for the last generation ("kurz zusagen"), if any.
    instruction: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(8))
    # Model and prompt of the last generation; ``None`` for drafts written by hand.
    model: Mapped[str | None] = mapped_column(String(255))
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    sent_at: Mapped[datetime | None]
    # ``Message-ID`` of the sent mail.
    sent_message_id: Mapped[str | None] = mapped_column(Text)


class DraftSettings(Base):
    """Drafting preferences of a user."""

    __tablename__ = "reply_draft_settings"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )
    # Appended to generated drafts.
    signature: Mapped[str] = mapped_column(Text, default="", server_default="")
    # Pass a few own sent mails as examples of the user's writing style.
    style_examples: Mapped[bool] = mapped_column(server_default=true(), default=True)
