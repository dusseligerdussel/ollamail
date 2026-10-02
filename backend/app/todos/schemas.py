"""API schemas for todos."""

import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

from app.todos.models import TodoPriority, TodoStatus

Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
Description = Annotated[str, StringConstraints(strip_whitespace=True, max_length=10_000)]


class TodoExportState(BaseModel):
    """Export of a todo to the user's task system (#40)."""

    sink: str
    # ``pending`` (waiting), ``synced``, ``error`` (rejected by the target, retried) or
    # ``removed`` (deleted in the target system, not exported again unless requested).
    state: Literal["pending", "synced", "error", "removed"]
    synced_at: datetime | None
    # Static error code, e.g. ``http_400``.
    error: str | None


class TodoRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    description: str | None
    due_date: date | None
    priority: TodoPriority
    status: TodoStatus
    is_manual: bool
    is_edited: bool
    confidence: float | None
    # A later mail suggests the todo is done; cleared when the status changes.
    done_suggested: bool
    # Source of an extracted todo (or the mail a manual todo refers to).
    mailbox_id: uuid.UUID | None
    message_id: uuid.UUID | None
    thread_id: uuid.UUID | None
    external_refs: dict[str, Any]
    export_state: TodoExportState | None
    # Team todo of a shared mailbox: visible to all its readers, assignable to one of them.
    shared: bool
    assignee_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class TodoCreate(BaseModel):
    """A todo created by the user, optionally linked to one of their mails."""

    title: Title
    description: Description | None = None
    due_date: date | None = None
    priority: TodoPriority = TodoPriority.NORMAL
    message_id: uuid.UUID | None = None


class TodoUpdate(BaseModel):
    """Fields to change; omitted fields stay. ``null`` clears description and due date."""

    title: Title | None = None
    description: Description | None = None
    due_date: date | None = None
    priority: TodoPriority | None = None
    status: TodoStatus | None = None
    # ``false`` dismisses a "done" suggestion without changing the status.
    done_suggested: bool | None = None
    # Team todos only: a reader of the shared mailbox, ``null`` for nobody.
    assignee_id: uuid.UUID | None = None
