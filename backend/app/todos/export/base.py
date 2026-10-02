"""The ``TodoSink`` interface: one external task system (CalDAV, later Microsoft To Do,
Google Tasks).

A sink only moves data; the sync (``app.todos.export.service``) decides what to send and
resolves conflicts. Remote tasks are addressed by an opaque ``remote_id`` (a CalDAV href,
a Graph or Google task ID) and versioned by an ``etag`` the sink returns on every write.
Errors carry a static code only, never a server message (docs/PRIVACY.md).
"""

import abc
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, ClassVar

from app.todos.models import TodoPriority, TodoStatus


@dataclass(frozen=True)
class TaskList:
    """A list (CalDAV: calendar collection with VTODO support) the user can export to."""

    id: str
    name: str


@dataclass(frozen=True)
class TaskData:
    """What is sent for one todo."""

    # Stable per todo and target system, e.g. the todo ID.
    uid: str
    title: str
    description: str | None
    due_date: date | None
    priority: TodoPriority
    status: TodoStatus
    completed_at: datetime | None
    # Last change of the todo; the remote copy carries it as its modification time.
    modified_at: datetime
    # Link to the source mail in the web UI, if known.
    url: str | None = None


@dataclass(frozen=True)
class RemoteVersion:
    """Where a written task lives and which version was written."""

    remote_id: str
    etag: str | None


@dataclass(frozen=True)
class RemoteTask:
    """The state of a remote task that changed since it was last written."""

    remote_id: str
    etag: str | None
    status: TodoStatus
    # Last change in the target system, if it says; ``None`` counts as "now".
    modified_at: datetime | None


class SinkError(Exception):
    """A request to the target failed. ``code`` is static and safe to store and log."""

    code: str = "sink_error"
    # Retrying the same request later can help (network, server busy).
    transient: bool = False

    def __init__(self, code: str | None = None) -> None:
        super().__init__(code or self.code)
        if code is not None:
            self.code = code


class SinkAuthError(SinkError):
    """Credentials rejected or missing permissions; the user has to reconnect."""

    code = "auth_failed"


class SinkNotFoundError(SinkError):
    """The list or task does not exist (any more)."""

    code = "not_found"


class SinkConflictError(SinkError):
    """The remote task changed since it was read (precondition failed)."""

    code = "conflict"


class SinkUnavailableError(SinkError):
    """Network error, timeout, server error or rate limit."""

    code = "unavailable"
    transient = True


class TodoSink(abc.ABC):
    """One connected account in one external task system."""

    # Target type, the key in ``Todo.external_refs`` and in the registry.
    kind: ClassVar[str]

    @abc.abstractmethod
    async def list_task_lists(self) -> list[TaskList]:
        """Lists the account can hold todos in; also checks the connection."""

    @abc.abstractmethod
    async def push(self, list_id: str, task: TaskData) -> RemoteVersion:
        """Create the task. Pushing the same ``uid`` again must not create a duplicate."""

    @abc.abstractmethod
    async def update(
        self, list_id: str, remote_id: str, etag: str | None, task: TaskData
    ) -> RemoteVersion:
        """Overwrite the task. Raises ``SinkConflictError`` if ``etag`` is outdated and
        ``SinkNotFoundError`` if the task was deleted in the target system."""

    async def complete(
        self, list_id: str, remote_id: str, etag: str | None, task: TaskData
    ) -> RemoteVersion:
        """Mark the task done; most systems do this with a regular update."""
        return await self.update(list_id, remote_id, etag, task)

    @abc.abstractmethod
    async def delete(self, list_id: str, remote_id: str) -> None:
        """Delete the task; a task that is already gone is no error."""

    @abc.abstractmethod
    async def changes(
        self, list_id: str, known: Mapping[str, str | None]
    ) -> dict[str, RemoteTask | None]:
        """Status sync: of the ``known`` tasks (remote ID → etag last written), those that
        changed in the target system (``RemoteTask``) or were deleted there (``None``).
        Unchanged tasks are left out."""

    def updated_config(self) -> Mapping[str, Any] | None:
        """The configuration to store after use if the sink changed it (rotated OAuth
        tokens, sync state), else ``None``."""
        return None

    async def aclose(self) -> None:  # noqa: B027 (optional hook)
        """Release connections."""
