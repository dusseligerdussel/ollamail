"""Retention settings and personal data exports."""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, Enum, ForeignKey, String, true
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class RetentionSettingsRecord(Base):
    """The single row of instance-wide retention periods (days, 0 keeps the data).

    ``NULL`` means: use the environment (``OLLAMAIL_PRIVACY_*``, ``OLLAMAIL_DIGEST_*``,
    ``OLLAMAIL_RAG_*``, ``OLLAMAIL_AUDIT_*``). The last run of ``privacy.retention`` is kept
    here as counters only, for the admin page.
    """

    __tablename__ = "privacy_retention_settings"
    __table_args__ = (CheckConstraint("singleton", name="singleton"),)

    singleton: Mapped[bool] = mapped_column(server_default=true(), default=True, unique=True)
    mail_days: Mapped[int | None]
    attachment_days: Mapped[int | None]
    search_index_days: Mapped[int | None]
    rag_history_days: Mapped[int | None]
    digest_days: Mapped[int | None]
    audit_days: Mapped[int | None]
    last_run_at: Mapped[datetime | None]
    # {"mails": 3, "attachments": 1, ...}
    last_run: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")


class ExportStatus(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    READY = "ready"
    FAILED = "failed"


class DataExport(Base):
    """A personal data export (Art. 15/20) of one user. The ZIP file lives under
    ``<data_dir>/exports/<user_id>/<export_id>.zip`` and is deleted at ``expires_at``."""

    __tablename__ = "privacy_exports"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[ExportStatus] = mapped_column(
        Enum(
            ExportStatus,
            name="privacy_export_status",
            native_enum=False,
            create_constraint=True,
            length=16,
            values_callable=lambda members: [member.value for member in members],
        ),
        default=ExportStatus.PENDING,
    )
    error_code: Mapped[str | None] = mapped_column(String(64))
    # Relative to the data directory; set when the file is ready.
    file_path: Mapped[str | None] = mapped_column(String(255))
    size: Mapped[int | None] = mapped_column(BigInteger)
    finished_at: Mapped[datetime | None]
    expires_at: Mapped[datetime | None] = mapped_column(index=True)
