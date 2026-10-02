"""Todos: extracted from mails (pipeline step ``todos``) or created by the user.

A todo belongs to one user and is only ever visible to them. Extracted todos keep a link
to their source: mailbox (``ON DELETE CASCADE``, so deleting a mailbox deletes its todos,
docs/PRIVACY.md), message and thread (``SET NULL``: the todo outlives a single deleted
mail, the user can still delete it).
"""

import enum
import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import Enum, Float, ForeignKey, Index, String, Text, false
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class TodoStatus(enum.StrEnum):
    OPEN = "open"
    DONE = "done"
    DISMISSED = "dismissed"


class TodoPriority(enum.StrEnum):
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


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


class Todo(Base):
    __tablename__ = "todos"
    __table_args__ = (
        Index("ix_todos_user_id_status_due_date", "user_id", "status", "due_date"),
        Index(None, "thread_id"),
        Index(None, "message_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    mailbox_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("mail_mailboxes.id", ondelete="CASCADE"), index=True
    )
    # Mail the todo was extracted from; later mails in the thread may update it.
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("mail_messages.id", ondelete="SET NULL")
    )
    thread_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("mail_threads.id", ondelete="SET NULL")
    )

    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    # Date in the user's time zone (relative phrases are resolved on extraction).
    due_date: Mapped[date | None]
    priority: Mapped[TodoPriority] = mapped_column(
        _str_enum(TodoPriority, "todo_priority"), default=TodoPriority.NORMAL
    )
    status: Mapped[TodoStatus] = mapped_column(
        _str_enum(TodoStatus, "todo_status"), default=TodoStatus.OPEN
    )
    completed_at: Mapped[datetime | None]

    # Created by the user, not extracted.
    is_manual: Mapped[bool] = mapped_column(server_default=false(), default=False)
    # Changed by the user: extraction no longer overwrites title, description, due date.
    is_edited: Mapped[bool] = mapped_column(server_default=false(), default=False)
    # Model confidence (0-1) of an extracted todo; ``None`` for manual ones.
    confidence: Mapped[float | None] = mapped_column(Float)
    # A later mail in the thread suggests the todo is done; the user decides.
    done_suggested: Mapped[bool] = mapped_column(server_default=false(), default=False)

    # IDs in external task systems (CalDAV, Microsoft To Do, Google Tasks; #40), e.g.
    # ``{"caldav": {"uid": "..."}}``.
    external_refs: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
