"""Conversations of "ask your inbox": questions, answers and their citations.

Deletion (docs/PRIVACY.md, Löschkonzept): a conversation belongs to one user and hangs off
``users`` with ``ON DELETE CASCADE``; its messages hang off the conversation, citations off
the answer. A citation stores an excerpt of a mail, so it also hangs off the mail and the
mailbox with ``ON DELETE CASCADE``: deleting a mail or mailbox removes its excerpts from
every conversation (the answer keeps its ``[n]`` marker, the source is then gone).
Conversations older than ``OLLAMAIL_RAG_HISTORY_RETENTION_DAYS`` are purged daily.
"""

import enum
import uuid
from typing import Any

from sqlalchemy import Enum, ForeignKey, Index, SmallInteger, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


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


class RagRole(enum.StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class AnswerStatus(enum.StrEnum):
    # The answer cites at least one retrieved source.
    ANSWERED = "answered"
    # Nothing relevant was found or the model cited no source: not backed by mails.
    NO_EVIDENCE = "no_evidence"


class RagConversation(Base):
    __tablename__ = "rag_conversations"
    __table_args__ = (Index("ix_rag_conversations_user_id_updated_at", "user_id", "updated_at"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # Beginning of the first question.
    title: Mapped[str] = mapped_column(String(200))

    messages: Mapped[list["RagMessage"]] = relationship(
        back_populates="conversation",
        order_by="RagMessage.position",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class RagMessage(Base):
    """A question of the user or an answer of the model."""

    __tablename__ = "rag_messages"
    __table_args__ = (UniqueConstraint("conversation_id", "position"),)

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("rag_conversations.id", ondelete="CASCADE")
    )
    # Order within the conversation (0, 1, ...): a question is followed by its answer.
    position: Mapped[int]
    role: Mapped[RagRole] = mapped_column(_str_enum(RagRole, "rag_role"))
    content: Mapped[str] = mapped_column(Text)
    # Question: the filters the search used (UI and extracted), see ``schemas.AppliedFilters``.
    filters: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Answer: status, model and prompt (``rag_answer@1``) for traceability.
    status: Mapped[AnswerStatus | None] = mapped_column(_str_enum(AnswerStatus, "rag_status"))
    model: Mapped[str | None] = mapped_column(String(255))
    prompt_version: Mapped[str | None] = mapped_column(String(64))

    conversation: Mapped[RagConversation] = relationship(back_populates="messages")
    citations: Mapped[list["RagCitation"]] = relationship(
        order_by="RagCitation.number", cascade="all, delete-orphan", passive_deletes=True
    )


class RagCitation(Base):
    """Source ``[number]`` of an answer: a chunk of a mail the search returned."""

    __tablename__ = "rag_citations"
    __table_args__ = (
        UniqueConstraint("answer_id", "number"),
        Index(None, "message_id"),
        Index(None, "mailbox_id"),
        Index(None, "attachment_id"),
    )

    answer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("rag_messages.id", ondelete="CASCADE"))
    number: Mapped[int] = mapped_column(SmallInteger)
    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("mail_messages.id", ondelete="CASCADE")
    )
    mailbox_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("mail_mailboxes.id", ondelete="CASCADE")
    )
    attachment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("mail_attachments.id", ondelete="CASCADE")
    )
    # ``body``, ``attachment`` or ``attachment_ocr`` (``app.search.models.ChunkSource``).
    source: Mapped[str] = mapped_column(String(16))
    # Header context of the chunk (sender, date, subject) and the excerpt shown to the user.
    heading: Mapped[str] = mapped_column(Text)
    snippet: Mapped[str] = mapped_column(Text)
