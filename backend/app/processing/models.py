"""Processing state per message and step, and the per-mailbox switch.

Both tables hang off the mail tables with ``ON DELETE CASCADE`` (docs/PRIVACY.md,
Löschkonzept). They hold no content: step names, versions, status and error codes only.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import Enum, ForeignKey, String, UniqueConstraint, false, true
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class StepStatus(enum.StrEnum):
    # Waiting for its dependencies or its job; also while a failed attempt awaits a retry.
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    # Gave up (permanent error or retries exhausted). Runs again by reprocessing, or
    # automatically at ``retry_at`` if the error was a passing one.
    FAILED = "failed"
    # Deliberately not run: the message is older than ``OLLAMAIL_PROCESSING_BACKFILL_LLM_DAYS``
    # and the step is ``recent_only`` (or depends on one). Runs once the mailbox opts in
    # ("classify older mails too").
    SKIPPED = "skipped"


class MessageProcessing(Base):
    """State of one step for one message."""

    __tablename__ = "message_processing"
    __table_args__ = (UniqueConstraint("message_id", "step"),)

    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("mail_messages.id", ondelete="CASCADE")
    )
    step: Mapped[str] = mapped_column(String(64))
    # Step version the status refers to; a newer registered version means "outdated".
    version: Mapped[int]
    status: Mapped[StepStatus] = mapped_column(
        Enum(
            StepStatus,
            name="step_status",
            native_enum=False,
            create_constraint=True,
            length=16,
            values_callable=lambda members: [member.value for member in members],
        ),
        default=StepStatus.PENDING,
    )
    # Machine-readable code of the last failure (``StepError.code`` or the exception
    # class name); never exception texts, which may contain mail content.
    error_code: Mapped[str | None] = mapped_column(String(64))
    # Runs of the current version, including retries.
    attempts: Mapped[int] = mapped_column(default=0, server_default="0")
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    # Failed for a passing reason (LLM down, model missing): ``processing.retry_failed``
    # runs the step again at this time. ``None`` for permanent failures.
    retry_at: Mapped[datetime | None] = mapped_column(index=True)
    # Automatic retries since the step last succeeded or was reset.
    auto_retries: Mapped[int] = mapped_column(default=0, server_default="0")


class MailboxProcessingSettings(Base):
    """Opt-out of automatic processing (e.g. no AI for one mailbox). No row = enabled,
    recent mails only for ``recent_only`` steps."""

    __tablename__ = "processing_mailbox_settings"

    mailbox_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("mail_mailboxes.id", ondelete="CASCADE"), unique=True
    )
    enabled: Mapped[bool] = mapped_column(server_default=true())
    # Run ``recent_only`` steps (triage, todos) for older mails too, ignoring
    # ``OLLAMAIL_PROCESSING_BACKFILL_LLM_DAYS`` ("classify older mails too").
    include_older: Mapped[bool] = mapped_column(server_default=false())
