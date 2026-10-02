"""Where a todo was exported to: ``Todo.external_refs[<sink>]``.

``{"target": <target id>, "list": <list id>, "id": <remote id>, "etag": ..., "state": ...,
"synced_at": <ISO time>, "error": <code>}``. ``synced_at`` equals ``todos.updated_at`` right
after a sync wrote the todo, so any later change (API, extraction) makes them differ: that
is how changed todos are found without a separate change log.

States: ``pending`` (waiting for the first or a requested export), ``synced``, ``error``
(the target rejected this todo; retried with the next status check), ``removed`` (deleted
in the target system; not exported again unless the user asks).
"""

import enum
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Any, Self


class RefState(enum.StrEnum):
    PENDING = "pending"
    SYNCED = "synced"
    ERROR = "error"
    REMOVED = "removed"


@dataclass(frozen=True)
class ExportRef:
    target: str
    list: str
    state: RefState
    id: str | None = None
    etag: str | None = None
    synced_at: str | None = None
    error: str | None = None

    @classmethod
    def from_json(cls, value: Any) -> Self | None:
        if not isinstance(value, dict):
            return None
        try:
            return cls(
                target=str(value["target"]),
                list=str(value["list"]),
                state=RefState(value.get("state", RefState.SYNCED)),
                id=value.get("id"),
                etag=value.get("etag"),
                synced_at=value.get("synced_at"),
                error=value.get("error"),
            )
        except (KeyError, ValueError):
            return None

    def to_json(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}

    def synced(self, remote_id: str, etag: str | None, at: datetime) -> "ExportRef":
        return replace(
            self,
            state=RefState.SYNCED,
            id=remote_id,
            etag=etag,
            synced_at=at.isoformat(),
            error=None,
        )

    def failed(self, code: str) -> "ExportRef":
        return replace(self, state=RefState.ERROR, error=code)

    def removed(self) -> "ExportRef":
        return replace(self, state=RefState.REMOVED, etag=None, error=None)

    def synced_time(self) -> datetime | None:
        if not self.synced_at:
            return None
        try:
            return datetime.fromisoformat(self.synced_at)
        except ValueError:
            return None


def read_ref(external_refs: dict[str, Any] | None, sink: str) -> ExportRef | None:
    return ExportRef.from_json((external_refs or {}).get(sink))


def with_ref(external_refs: dict[str, Any] | None, sink: str, ref: ExportRef) -> dict[str, Any]:
    """A copy with ``ref`` set (JSONB columns need a new object to register a change)."""
    return {**(external_refs or {}), sink: ref.to_json()}


def export_state(external_refs: dict[str, Any] | None) -> dict[str, Any] | None:
    """Export state of a todo for the API: sink, state, time of the last sync, error code."""
    for sink, value in (external_refs or {}).items():
        ref = read_ref({sink: value}, sink)
        if ref is not None:
            return {
                "sink": sink,
                "state": ref.state.value,
                "synced_at": ref.synced_time(),
                "error": ref.error,
            }
    return None
