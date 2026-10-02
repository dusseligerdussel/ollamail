"""Request and response schemas of the mailbox API.

Credentials are write-only: requests may carry them, responses only say whether some are
stored (``has_credentials``).
"""

import json
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.mail.api.autodiscovery import Hint, Source
from app.mail.models import FolderKind, FolderRole, MailboxType
from app.mail.schemas import SyncSettings

# Deliberately loose (internationalised addresses, IMAP logins that look like addresses);
# the connection test is the real check.
Address = Annotated[
    str, StringConstraints(strip_whitespace=True, pattern=r"^[^@\s]+@[^@\s]+$", max_length=320)
]
DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
SecretValue = Annotated[str, StringConstraints(max_length=16_384)]
Credentials = Annotated[dict[str, SecretValue], Field(max_length=16)]

# Upper bound for the provider settings JSON (non-secret connection parameters).
MAX_SETTINGS_BYTES = 16_384


def _bounded_settings(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is not None and len(json.dumps(value)) > MAX_SETTINGS_BYTES:
        raise ValueError("provider settings too large")
    return value


class MailboxConnection(BaseModel):
    """Everything needed to connect to a mailbox."""

    type: MailboxType
    address: Address
    # Provider-specific, non-secret settings (IMAP: host, port, security, ...).
    provider_settings: dict[str, Any] = Field(default_factory=dict)
    # Provider-specific secrets (IMAP: ``password`` or ``access_token``); stored encrypted.
    credentials: Credentials = Field(default_factory=dict)

    _check_settings = field_validator("provider_settings")(_bounded_settings)


class MailboxCreate(MailboxConnection):
    # Default: the address.
    display_name: DisplayName | None = None
    # Import period, excluded roles and folders (e.g. from the connection test), polling.
    sync_settings: SyncSettings = Field(default_factory=SyncSettings)
    # ``false`` adds the mailbox paused.
    sync_enabled: bool = True


class SyncSettingsUpdate(BaseModel):
    """Changes to ``SyncSettings``; omitted fields stay, ``initial_sync_days: null`` falls
    back to the instance default. The import period applies to folders whose initial
    import has not started yet."""

    initial_sync_days: int | None = Field(default=None, ge=1)
    excluded_roles: list[FolderRole] | None = None
    excluded_folders: list[str] | None = None
    poll_interval_seconds: int | None = Field(default=None, ge=30)


class MailboxUpdate(BaseModel):
    """Fields to change; omitted fields stay. Changed connection settings or credentials
    are tested before they are saved. ``sync_enabled`` pauses or resumes syncing."""

    display_name: DisplayName | None = None
    # Replaces the provider settings as a whole.
    provider_settings: dict[str, Any] | None = None
    # Replaces the stored credentials as a whole.
    credentials: Credentials | None = None
    sync_settings: SyncSettingsUpdate | None = None
    sync_enabled: bool | None = None

    _check_settings = field_validator("provider_settings")(_bounded_settings)


class RemoteFolderRead(BaseModel):
    """A folder as reported by the server (connection test)."""

    remote_id: str
    name: str
    kind: FolderKind
    role: FolderRole | None
    parent_id: str | None


class ConnectionTestResult(BaseModel):
    ok: bool
    # Error code (e.g. ``authentication_failed``, ``connection_failed``); never server text.
    error: str | None = None
    folders: list[RemoteFolderRead] = Field(default_factory=list)


SyncPhase = Literal["paused", "pending", "importing", "syncing", "idle", "error"]


class MailboxSyncStatus(BaseModel):
    """``phase`` summarises the fields: ``paused`` (sync disabled), ``error`` (the last sync
    failed for the whole mailbox), ``syncing`` (a sync job is queued or running),
    ``pending`` (never synced), ``importing`` (initial import of some folder unfinished),
    else ``idle``."""

    phase: SyncPhase
    # Last complete sync of the mailbox.
    last_synced_at: datetime | None
    # Error code of the last sync if it failed for the whole mailbox.
    last_error: str | None
    # A sync job is queued or running.
    sync_queued: bool
    # Folders selected for syncing, of which the initial import is finished resp. failed.
    folders_total: int
    folders_imported: int
    folders_failed: int
    message_count: int


class MailboxRead(BaseModel):
    id: uuid.UUID
    type: MailboxType
    display_name: str
    address: str
    is_shared: bool
    provider_settings: dict[str, Any]
    has_credentials: bool
    sync_enabled: bool
    sync_settings: SyncSettings
    status: MailboxSyncStatus
    created_at: datetime
    updated_at: datetime


class FolderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    remote_id: str
    name: str
    kind: FolderKind
    role: FolderRole | None
    # Selected by the user.
    sync_enabled: bool
    # Excluded through ``SyncSettings.excluded_roles`` (e.g. trash, spam).
    excluded_by_role: bool
    # Effectively synced: selected and not excluded.
    synced: bool
    last_synced_at: datetime | None
    last_error: str | None
    # The initial import has not finished yet.
    import_pending: bool
    message_count: int


class FolderSelection(BaseModel):
    id: uuid.UUID
    sync_enabled: bool


class FolderSelectionUpdate(BaseModel):
    folders: list[FolderSelection] = Field(min_length=1, max_length=10_000)


class SyncRequestResult(BaseModel):
    # ``false``: a sync was already waiting in the queue.
    queued: bool


class MailboxDeleted(BaseModel):
    """Confirmation of a removal: the mailbox and all data derived from it (mails,
    attachments and their files, threads, folders, sync state, processing results, todos,
    triage results, search index) are deleted."""

    mailbox_id: uuid.UUID
    deleted: Literal[True] = True
    messages: int
    attachments: int


class AutodiscoverRequest(BaseModel):
    address: Address


class AutodiscoverSuggestion(BaseModel):
    type: MailboxType
    provider_settings: dict[str, Any]
    source: Source
    hints: list[Hint]


class AutodiscoverResult(BaseModel):
    suggestions: list[AutodiscoverSuggestion]
