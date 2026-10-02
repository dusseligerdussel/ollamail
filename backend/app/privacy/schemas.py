"""API schemas for retention, data export and account deletion."""

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.privacy.models import ExportStatus

# Up to 100 years; 0 keeps the data.
Days = Annotated[int, Field(ge=0, le=36500)]


class RetentionValues(BaseModel):
    """Retention in days per data category; 0 keeps the data."""

    # Mails with attachments, search index, triage results and citations.
    mail_days: Days
    # Attachment files only.
    attachment_days: Days
    # Search index (text chunks and embeddings) only.
    search_index_days: Days
    # "Ask your inbox" conversations, after the last question.
    rag_history_days: Days
    # Digests with their audio; at least one day.
    digest_days: Annotated[int, Field(ge=1, le=3650)]
    # Audit log.
    audit_days: Days


class RetentionRun(BaseModel):
    """Counters of the last run of the retention job."""

    finished_at: datetime
    mails: int = 0
    attachments: int = 0
    search_chunks: int = 0
    threads: int = 0
    audit_events: int = 0


class RetentionSettingsRead(BaseModel):
    """Effective retention, the environment defaults and the last run."""

    values: RetentionValues
    defaults: RetentionValues
    # Fields set in the admin area (others follow the environment).
    overridden: list[str]
    # Initial import window of new mailboxes; a shorter mail retention makes a full
    # resync import mails that are deleted again by the next run.
    initial_sync_days: int
    last_run: RetentionRun | None


class RetentionSettingsUpdate(BaseModel):
    """New values; ``null`` resets a field to the environment default. Omitted fields
    keep their value."""

    model_config = ConfigDict(extra="forbid")

    mail_days: Days | None = None
    attachment_days: Days | None = None
    search_index_days: Days | None = None
    rag_history_days: Days | None = None
    digest_days: Annotated[int, Field(ge=1, le=3650)] | None = None
    audit_days: Days | None = None


class DataExportRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: ExportStatus
    error_code: str | None
    size: int | None
    created_at: datetime
    finished_at: datetime | None
    # Download possible until then (ready exports); afterwards the file is deleted.
    expires_at: datetime | None


class AccountPrivacyRead(BaseModel):
    """What the account page offers."""

    self_delete_enabled: bool
    export_expiry_hours: int


class AccountDeletion(BaseModel):
    """Confirmation of the own account's deletion: the account's e-mail address."""

    model_config = ConfigDict(extra="forbid")

    confirm_email: str = Field(max_length=320)


class UserDeletionResult(BaseModel):
    user_id: uuid.UUID
    deleted: bool
    mailboxes: int
