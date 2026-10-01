"""Typed views of the JSON columns of ``Mailbox``."""

from pydantic import BaseModel, ConfigDict, Field

from app.mail.models import FolderRole


class SyncSettings(BaseModel):
    """``Mailbox.sync_settings``. Unset values fall back to instance settings."""

    model_config = ConfigDict(extra="ignore")

    # Days of mail to import initially; ``None`` = ``OLLAMAIL_MAIL_INITIAL_SYNC_DAYS``.
    initial_sync_days: int | None = Field(default=None, ge=1)
    # Folder roles that are never synced (data minimisation).
    excluded_roles: list[FolderRole] = Field(
        default_factory=lambda: [FolderRole.TRASH, FolderRole.JUNK]
    )
    # Remote folder IDs that are excluded in addition.
    excluded_folders: list[str] = Field(default_factory=list)
    # Fall back to polling if push is unavailable; seconds between polls.
    poll_interval_seconds: int = Field(default=300, ge=30)
