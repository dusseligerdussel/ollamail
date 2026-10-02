"""API schemas for the todo export settings."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

from app.todos.export.models import ExportMode

# Target types; new sinks are added here and in ``registry.FACTORIES``.
SinkKind = Literal["caldav", "gtasks"]
# Connected with OAuth (own connect flow), not with the URL/credentials form.
OAUTH_SINKS: frozenset[str] = frozenset({"gtasks"})
ServerUrl = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2048)]
Username = Annotated[str, StringConstraints(strip_whitespace=True, max_length=255)]
Password = Annotated[str, StringConstraints(max_length=1024)]
ListId = Annotated[str, StringConstraints(min_length=1, max_length=2048)]


class ExportConnection(BaseModel):
    """How to reach the target. ``password`` ``null``: keep the stored one (same server
    and user name)."""

    sink: SinkKind
    url: ServerUrl
    username: Username = ""
    password: Password | None = None


class TaskListRead(BaseModel):
    id: str
    name: str


class ExportTargetSave(ExportConnection):
    """Connect (or reconnect) the export: target, list and mode."""

    list_id: ListId
    mode: ExportMode = ExportMode.AUTO


class ExportTargetUpdate(BaseModel):
    """Fields left out stay as they are."""

    mode: ExportMode | None = None
    # Another list of the connected account.
    list_id: ListId | None = None


class ExportCounts(BaseModel):
    """Todos of this target by export state."""

    synced: int = 0
    pending: int = 0
    error: int = 0
    removed: int = 0


class ExportTargetRead(BaseModel):
    sink: SinkKind
    url: str
    username: str
    # Credentials are never returned, only whether a password is stored.
    has_password: bool
    list_id: str
    list_name: str
    mode: ExportMode
    # Allowed by the admin (``OLLAMAIL_TODOS_EXPORT_SINKS``); otherwise nothing is synced.
    active: bool
    last_sync_at: datetime | None
    # Static code of the last failed sync, e.g. ``auth_failed``, ``unavailable``.
    last_error: str | None
    counts: ExportCounts
    created_at: datetime


class ExportSettingsRead(BaseModel):
    # Target types the admin allows; empty: export is switched off on this instance.
    available_sinks: list[SinkKind] = Field(default_factory=list)
    target: ExportTargetRead | None
