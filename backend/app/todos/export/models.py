"""Export settings of a user: one target system, one list, automatic or manual export.

The row hangs on the user (``ON DELETE CASCADE``); server URL and credentials are
encrypted (``EncryptedJSON``, docs/PRIVACY.md). Which todo went where is stored on the
todo itself (``Todo.external_refs[<sink>]``, see ``app.todos.export.refs``).
"""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.crypto import EncryptedJSON
from app.core.db import Base


class ExportMode(enum.StrEnum):
    # Every open todo is exported, new ones as soon as they appear.
    AUTO = "auto"
    # Only todos the user exports one by one; they stay in sync afterwards.
    MANUAL = "manual"


class TodoExportTarget(Base):
    __tablename__ = "todo_export_targets"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )
    # Registry key of the sink, e.g. ``caldav``.
    sink: Mapped[str] = mapped_column(String(32))
    # Sink configuration incl. credentials, e.g. ``{"url", "username", "password"}``.
    config: Mapped[dict[str, Any]] = mapped_column(EncryptedJSON)
    # Chosen list (CalDAV: collection path) and its name as shown by the target system.
    list_id: Mapped[str] = mapped_column(String(2048))
    list_name: Mapped[str] = mapped_column(String(255))
    mode: Mapped[ExportMode] = mapped_column(
        Enum(
            ExportMode,
            name="todo_export_mode",
            native_enum=False,
            create_constraint=True,
            length=16,
            values_callable=lambda members: [member.value for member in members],
        ),
        default=ExportMode.AUTO,
    )
    # Origin of the web UI when the target was saved, for links to the mail when
    # ``OLLAMAIL_AUTH_PUBLIC_URL`` is not set.
    app_url: Mapped[str | None] = mapped_column(String(255))
    last_sync_at: Mapped[datetime | None]
    # Status check of exported todos due (``OLLAMAIL_TODOS_EXPORT_POLL_MINUTES``).
    next_poll_at: Mapped[datetime | None]
    # ``{"list", "id"}`` of exported todos deleted in ollamail, removed in the target by the
    # next sync. Encrypted: CalDAV paths can contain the user name.
    pending_deletions: Mapped[list[dict[str, str]] | None] = mapped_column(EncryptedJSON)
    # Static code of the last failed sync (``SinkError.code``); ``None`` after a good one.
    last_error: Mapped[str | None] = mapped_column(String(64))
