"""Triage data model: categories, per-user preferences, results, corrections, sender rules
and the per-mailbox write-back switch.

Deletion (docs/PRIVACY.md, Löschkonzept): every table hangs off ``users``, ``mail_messages``
or ``mail_mailboxes`` with ``ON DELETE CASCADE``. Results and corrections are deleted with
their message (and so with the mailbox); preferences, own categories and sender rules with
their user. Organisation categories (``owner_user_id IS NULL``) belong to the instance.

Privacy: corrections (``TriageFeedback``) are few-shot examples of exactly one user and are
only ever read filtered by ``user_id`` (``app.triage.feedback``).
"""

import enum
import uuid

from sqlalchemy import (
    CheckConstraint,
    Enum,
    Float,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    false,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
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


class TriageSource(enum.StrEnum):
    """Who decided the category of a message."""

    # Header rules of the pre-filter (List-Unsubscribe, Precedence, Auto-Submitted, ...).
    RULE = "rule"
    # A sender rule of the user ("always Newsletter").
    SENDER_RULE = "sender_rule"
    LLM = "llm"
    # Corrected by the user; never overwritten by reprocessing.
    USER = "user"


class WriteBackMode(enum.StrEnum):
    """What the triage writes back to the mail server (opt-in per mailbox)."""

    OFF = "off"
    # Keyword (IMAP), label (Gmail) or category (Graph), see ``MailProvider.apply_label``.
    LABEL = "label"
    # Move into an existing folder named after the category.
    MOVE = "move"


class TriageCategory(Base):
    """A category: organisation default (``owner_user_id IS NULL``) or a user's own one."""

    __tablename__ = "triage_categories"
    __table_args__ = (Index(None, "owner_user_id"),)

    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    # Set for the built-in defaults (``important``, ``newsletter``, ...) as long as the
    # admin has not renamed them; the UI translates these by key.
    builtin_key: Mapped[str | None] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(100))
    # Natural-language description, part of the classification prompt.
    description: Mapped[str] = mapped_column(Text, default="")
    # Default order (ascending); users may reorder via ``TriageCategoryPreference``.
    position: Mapped[int] = mapped_column(default=0, server_default="0")


class TriageCategoryPreference(Base):
    """A user's order and visibility of a category (also of organisation categories)."""

    __tablename__ = "triage_category_preferences"
    __table_args__ = (UniqueConstraint("user_id", "category_id"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    category_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("triage_categories.id", ondelete="CASCADE"), index=True
    )
    # Hidden categories are neither shown nor offered to the model.
    hidden: Mapped[bool] = mapped_column(server_default=false(), default=False)
    position: Mapped[int | None]


class TriageResult(Base):
    """The triage of one message."""

    __tablename__ = "triage_results"
    __table_args__ = (
        CheckConstraint("priority BETWEEN 1 AND 3", name="priority"),
        Index(None, "category_id"),
        Index(
            "ix_triage_results_write_back_pending",
            "updated_at",
            postgresql_where=text("write_back_pending"),
        ),
    )

    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("mail_messages.id", ondelete="CASCADE"), unique=True
    )
    # ``NULL`` once the category was deleted; the message then counts as uncategorised.
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("triage_categories.id", ondelete="SET NULL")
    )
    # 1 = high, 2 = normal, 3 = low.
    priority: Mapped[int] = mapped_column(SmallInteger)
    # One sentence by the model (shown to the user, never logged).
    reason: Mapped[str | None] = mapped_column(Text)
    # Machine-readable rule for decisions without the model (``list_unsubscribe``, ...).
    rule: Mapped[str | None] = mapped_column(String(32))
    source: Mapped[TriageSource] = mapped_column(_str_enum(TriageSource, "triage_source"))
    # Model and prompt (``triage@1``) of LLM decisions, for traceability.
    model: Mapped[str | None] = mapped_column(String(255))
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    # Keyword/label/folder last written to the server, so a change can replace it.
    remote_label: Mapped[str | None] = mapped_column(Text)
    # The category changed since the last write-back (``app.triage.writeback``).
    write_back_pending: Mapped[bool] = mapped_column(server_default=false(), default=False)

    category: Mapped[TriageCategory | None] = relationship()


class TriageFeedback(Base):
    """A user's correction of one message: few-shot example for this user only.

    The example text is read from the message itself, so it disappears with it.
    """

    __tablename__ = "triage_feedback"
    __table_args__ = (
        UniqueConstraint("user_id", "message_id"),
        CheckConstraint("priority BETWEEN 1 AND 3", name="priority"),
        Index(None, "message_id"),
        Index(None, "category_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("mail_messages.id", ondelete="CASCADE")
    )
    category_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("triage_categories.id", ondelete="CASCADE")
    )
    priority: Mapped[int] = mapped_column(SmallInteger)
    # Embedding of the example text, computed lazily by the triage job.
    embedding: Mapped[list[float] | None] = mapped_column(ARRAY(Float))
    embedding_model: Mapped[str | None] = mapped_column(String(255))


class TriageSenderRule(Base):
    """ "Mails from <sender> are always <category>": applied by the pre-filter."""

    __tablename__ = "triage_sender_rules"
    __table_args__ = (
        UniqueConstraint("user_id", "sender"),
        CheckConstraint("priority BETWEEN 1 AND 3", name="priority"),
        Index(None, "category_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # Lower-case address (``news@example.org``) or domain with ``@`` (``@example.org``).
    sender: Mapped[str] = mapped_column(String(320))
    category_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("triage_categories.id", ondelete="CASCADE")
    )
    priority: Mapped[int] = mapped_column(SmallInteger, default=3)


class TriageMailboxSettings(Base):
    """Write-back of the category to the server; no row = off."""

    __tablename__ = "triage_mailbox_settings"

    mailbox_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("mail_mailboxes.id", ondelete="CASCADE"), unique=True
    )
    write_back: Mapped[WriteBackMode] = mapped_column(
        _str_enum(WriteBackMode, "triage_write_back"), default=WriteBackMode.OFF
    )
