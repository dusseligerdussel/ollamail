"""Daily digest data model: per-user settings (including the podcast feed token) and the
generated digests.

Deletion (docs/PRIVACY.md, Löschkonzept): both tables hang off ``users`` with ``ON DELETE
CASCADE``. Audio files live outside the database under ``<data_dir>/digests/<user_id>/``;
the cleanup job (``app.digest.tasks.cleanup``) deletes expired digests with their files,
digests whose source mailbox was removed, and files without a digest (e.g. of a deleted
user). Mailboxes are referenced by ID only (``mailbox_ids``), so a digest never blocks or
outlives the deletion of a mailbox for longer than one cleanup run.
"""

import enum
import uuid
from datetime import datetime, time
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Enum,
    Float,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    Time,
    UniqueConstraint,
    Uuid,
    false,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

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


class DigestLength(enum.StrEnum):
    SHORT = "short"
    NORMAL = "normal"


class DigestStatus(enum.StrEnum):
    # Created, the text job is queued.
    PENDING = "pending"
    # The summary is being written (LLM).
    SUMMARIZING = "summarizing"
    # The script is done, audio is being synthesised (TTS).
    SYNTHESIZING = "synthesizing"
    READY = "ready"
    FAILED = "failed"


class DigestTrigger(enum.StrEnum):
    SCHEDULED = "scheduled"
    MANUAL = "manual"


# Monday = 0 ... Sunday = 6 (``date.weekday()``).
ALL_WEEKDAYS = [0, 1, 2, 3, 4, 5, 6]


class DigestUserSettings(Base):
    """A user's digest settings; no row = defaults, digest off."""

    __tablename__ = "digest_user_settings"
    __table_args__ = (
        CheckConstraint("weekdays <@ ARRAY[0,1,2,3,4,5,6]::smallint[]", name="weekdays"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )
    enabled: Mapped[bool] = mapped_column(server_default=false(), default=False)
    # Local time of day in ``timezone``.
    delivery_time: Mapped[time] = mapped_column(Time, default=time(7, 0))
    # IANA time zone; ``NULL`` = the user's profile time zone.
    timezone: Mapped[str | None] = mapped_column(String(64))
    weekdays: Mapped[list[int]] = mapped_column(
        ARRAY(SmallInteger), default=lambda: list(ALL_WEEKDAYS)
    )
    # ``de``/``en``; ``NULL`` = the user's profile language.
    language: Mapped[str | None] = mapped_column(String(8))
    # TTS voice ID; ``NULL`` = the default voice of the language.
    voice: Mapped[str | None] = mapped_column(String(128))
    length: Mapped[DigestLength] = mapped_column(
        _str_enum(DigestLength, "digest_length"), default=DigestLength.NORMAL
    )
    # Mailboxes to include; ``NULL`` = all mailboxes the user can read.
    mailbox_ids: Mapped[list[uuid.UUID] | None] = mapped_column(ARRAY(Uuid))
    # The scheduled slot (UTC) last queued, so every slot runs once.
    last_scheduled_for: Mapped[datetime | None]
    # SHA-256 (hex) of the podcast feed token; the token itself is never stored.
    feed_token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    feed_token_created_at: Mapped[datetime | None]


class Digest(Base):
    __tablename__ = "digests"
    __table_args__ = (
        # A scheduled slot produces at most one digest.
        UniqueConstraint("user_id", "scheduled_for", name="uq_digests_user_id_scheduled_for"),
        Index("ix_digests_user_id_created_at", "user_id", "created_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    trigger: Mapped[DigestTrigger] = mapped_column(_str_enum(DigestTrigger, "digest_trigger"))
    status: Mapped[DigestStatus] = mapped_column(
        _str_enum(DigestStatus, "digest_status"), default=DigestStatus.PENDING
    )
    # Machine-readable failure code; never exception texts.
    error_code: Mapped[str | None] = mapped_column(String(64))
    # Slot of a scheduled digest (UTC); ``NULL`` for manual ones.
    scheduled_for: Mapped[datetime | None]
    # Mails received in [period_start, period_end) are covered.
    period_start: Mapped[datetime]
    period_end: Mapped[datetime]
    language: Mapped[str] = mapped_column(String(8))
    voice: Mapped[str | None] = mapped_column(String(128))
    length: Mapped[DigestLength] = mapped_column(_str_enum(DigestLength, "digest_length"))
    # Mailboxes the digest covers (IDs only, see the module docstring).
    mailbox_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid), default=list)

    title: Mapped[str] = mapped_column(String(255), default="")
    # Markdown; ``[n]`` marks refer to ``references``.
    script: Mapped[str | None] = mapped_column(Text)
    # ``[{"ref": n, "message_id": "...", "mailbox_id": "..."}]``; IDs only.
    references: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default="[]"
    )
    message_count: Mapped[int] = mapped_column(default=0, server_default="0")
    todo_count: Mapped[int] = mapped_column(default=0, server_default="0")
    model: Mapped[str | None] = mapped_column(String(255))
    prompt_version: Mapped[str | None] = mapped_column(String(64))

    # ``{"mp3": {"path": "digests/<user>/<id>.mp3", "size_bytes": 123}, ...}``; paths are
    # relative to the data directory and contain IDs only.
    audio: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    generated_at: Mapped[datetime | None]

    @property
    def audio_formats(self) -> list[str]:
        return sorted(self.audio or {})
