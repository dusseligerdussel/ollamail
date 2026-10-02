"""Google Tasks sink (Tasks API v1, #102): todos as tasks in one of the user's task lists.

* **Auth** reuses the Gmail building blocks (``app/mail/providers/gmail_auth.py``): the
  instance's OAuth client (``OLLAMAIL_GMAIL_CLIENT_ID``/``_SECRET``), a refresh token with
  the scope ``tasks`` stored encrypted in ``todo_export_targets.config`` (connect flow in
  ``gtasks_connect.py``) and access tokens in process memory only. Requests, token refresh
  after a 401 and retries of 429/5xx go through ``GoogleApiClient`` (``gmail_api.py``).
* **IDs:** a list is a task list ID, a task the Google task ID; ``etag`` is the task's
  ``etag``. Updates send ``If-Match`` (412 = conflict).
* **No duplicates:** Google assigns task IDs, so a create cannot be made idempotent by
  itself. The notes end with a marker ``[ollamail:<todo id>]``; ``push`` looks the uid up in
  the list first (one listing per sync run) and overwrites a task found there. The create
  itself is never retried after an answer got lost.
* **Mapping:** title, notes (description, link to the mail, marker), ``due`` (date only,
  Google ignores the time), ``status`` ``needsAction``/``completed``. Google Tasks has no
  priority and no "cancelled": dismissed todos are sent as completed.
* **Status sync** (``changes``): one listing of the list with ``showDeleted``,
  ``showHidden`` and ``showCompleted``; tasks whose ``etag`` differs changed, deleted or
  missing ones are gone. ``updatedMin`` is not used: the sink keeps no state between runs,
  and without a full listing a purged task could not be told from an unchanged one.

Errors are ``SinkError`` codes; Google's error texts and task contents are never logged.
"""

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, ClassVar
from urllib.parse import quote

import httpx

from app.core.config import GmailSettings
from app.mail.providers.base import (
    AuthenticationError,
    ConfigurationError,
    ConnectionFailedError,
    ProviderError,
)
from app.mail.providers.gmail_api import ConflictError, GoogleApiClient, NotFoundError
from app.mail.providers.gmail_auth import RefreshTokenSource, TokenSource
from app.todos.export.base import (
    RemoteTask,
    RemoteVersion,
    SinkAuthError,
    SinkConflictError,
    SinkError,
    SinkNotFoundError,
    SinkUnavailableError,
    TaskData,
    TaskList,
    TodoSink,
)
from app.todos.models import TodoStatus

TASKS_API = "https://tasks.googleapis.com/tasks/v1"
TASKS_HOST = "https://tasks.googleapis.com"
# Google's limits: title 1024, notes 8192 characters.
MAX_TITLE = 1024
MAX_NOTES = 8192
PAGE_SIZE = 100
# Upper bound for one listing (100 pages); larger lists are cut off, not loaded forever.
MAX_PAGES = 100
_MARKER = re.compile(r"\[ollamail:([0-9A-Za-z-]{1,64})\]\s*$")


def marker(uid: str) -> str:
    return f"[ollamail:{uid}]"


def build_notes(task: TaskData) -> str:
    """Description, link to the mail and the marker; the description is cut to fit."""
    tail = "\n".join(part for part in (task.url, marker(task.uid)) if part)
    description = (task.description or "").strip()
    room = MAX_NOTES - len(tail) - 2
    if len(description) > room:
        description = description[: max(room - 1, 0)].rstrip() + "…"
    return f"{description}\n\n{tail}" if description else tail


def uid_of(notes: Any) -> str | None:
    if not isinstance(notes, str):
        return None
    found = _MARKER.search(notes)
    return found.group(1) if found else None


def _rfc3339(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def task_body(task: TaskData) -> dict[str, Any]:
    done = task.status != TodoStatus.OPEN
    completed = task.completed_at or task.modified_at
    return {
        "title": (task.title.strip() or "-")[:MAX_TITLE],
        "notes": build_notes(task),
        # Date only: Google keeps the date part and drops the time.
        "due": f"{task.due_date.isoformat()}T00:00:00.000Z" if task.due_date else None,
        "status": "completed" if done else "needsAction",
        "completed": _rfc3339(completed) if done else None,
    }


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _sink_error(exc: ProviderError) -> SinkError:
    if isinstance(exc, AuthenticationError):
        return SinkAuthError()
    if isinstance(exc, ConnectionFailedError):
        return SinkUnavailableError()
    if isinstance(exc, NotFoundError):
        return SinkNotFoundError()
    if isinstance(exc, ConflictError):
        return SinkConflictError()
    if isinstance(exc, ConfigurationError):
        # ``oauth_not_configured``, ``api_disabled``: the admin has to act.
        return SinkError(exc.code)
    return SinkError("invalid_response")


def _segment(value: str) -> str:
    return quote(value, safe="")


class GoogleTasksSink(TodoSink):
    kind: ClassVar[str] = "gtasks"

    def __init__(
        self,
        refresh_token: str,
        gmail: GmailSettings,
        *,
        timeout: float,
        tokens: TokenSource | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._refresh_token = refresh_token
        self._gmail = gmail
        self._http = http or httpx.AsyncClient(timeout=timeout, follow_redirects=False)
        self._tokens = tokens
        self._client: GoogleApiClient | None = None
        # Per list: todo uid → (task id, etag) of the tasks carrying our marker.
        self._index: dict[str, dict[str, tuple[str, str | None]]] = {}

    async def aclose(self) -> None:
        await self._http.aclose()

    def _api(self) -> GoogleApiClient:
        if self._client is None:
            if not self._refresh_token:
                raise SinkAuthError()
            try:
                tokens = self._tokens or RefreshTokenSource(
                    self._http, self._gmail, self._refresh_token
                )
            except ProviderError as exc:
                raise _sink_error(exc) from None
            self._client = GoogleApiClient(self._http, tokens, TASKS_API, host=TASKS_HOST)
        return self._client

    async def _call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        api = self._api()
        try:
            return await api.request(method, path, **kwargs)
        except ProviderError as exc:
            raise _sink_error(exc) from None

    async def _pages(self, path: str, params: dict[str, str]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        token: str | None = None
        for _ in range(MAX_PAGES):
            query = {**params, "maxResults": str(PAGE_SIZE)}
            if token:
                query["pageToken"] = token
            data = await self._call("GET", path, params=query)
            items += [item for item in data.get("items") or () if isinstance(item, dict)]
            next_token = data.get("nextPageToken")
            if not isinstance(next_token, str) or not next_token:
                break
            token = next_token
        return items

    async def list_task_lists(self) -> list[TaskList]:
        items = await self._pages("/users/@me/lists", {})
        return [
            TaskList(id=str(item["id"]), name=str(item.get("title") or item["id"]))
            for item in items
            if isinstance(item.get("id"), str)
        ]

    def _tasks_path(self, list_id: str, remote_id: str | None = None) -> str:
        path = f"/lists/{_segment(list_id)}/tasks"
        return f"{path}/{_segment(remote_id)}" if remote_id is not None else path

    async def _all_tasks(self, list_id: str) -> list[dict[str, Any]]:
        return await self._pages(
            self._tasks_path(list_id),
            {"showCompleted": "true", "showHidden": "true", "showDeleted": "true"},
        )

    async def _known_uids(self, list_id: str) -> dict[str, tuple[str, str | None]]:
        index = self._index.get(list_id)
        if index is None:
            index = {}
            for item in await self._all_tasks(list_id):
                uid = uid_of(item.get("notes"))
                if uid and not item.get("deleted") and isinstance(item.get("id"), str):
                    etag = item.get("etag")
                    index[uid] = (item["id"], etag if isinstance(etag, str) else None)
            self._index[list_id] = index
        return index

    def _version(self, data: Mapping[str, Any], fallback_id: str | None = None) -> RemoteVersion:
        remote_id = data.get("id") if isinstance(data.get("id"), str) else fallback_id
        if not remote_id:
            raise SinkError("invalid_response")
        etag = data.get("etag")
        return RemoteVersion(str(remote_id), etag if isinstance(etag, str) else None)

    async def push(self, list_id: str, task: TaskData) -> RemoteVersion:
        index = await self._known_uids(list_id)
        existing = index.get(task.uid)
        if existing is not None:
            # Created before, the answer got lost: overwrite instead of duplicating.
            version = await self.update(list_id, existing[0], None, task)
        else:
            data = await self._call(
                "POST", self._tasks_path(list_id), json=task_body(task), retry=False
            )
            version = self._version(data)
        index[task.uid] = (version.remote_id, version.etag)
        return version

    async def update(
        self, list_id: str, remote_id: str, etag: str | None, task: TaskData
    ) -> RemoteVersion:
        headers = {"If-Match": etag} if etag else None
        data = await self._call(
            "PATCH", self._tasks_path(list_id, remote_id), json=task_body(task), headers=headers
        )
        if data.get("deleted"):
            raise SinkNotFoundError()
        return self._version(data, remote_id)

    async def delete(self, list_id: str, remote_id: str) -> None:
        try:
            await self._call("DELETE", self._tasks_path(list_id, remote_id))
        except SinkNotFoundError:
            return

    async def changes(
        self, list_id: str, known: Mapping[str, str | None]
    ) -> dict[str, RemoteTask | None]:
        if not known:
            return {}
        items = await self._all_tasks(list_id)
        remote = {item["id"]: item for item in items if isinstance(item.get("id"), str)}
        result: dict[str, RemoteTask | None] = {}
        for remote_id, etag in known.items():
            item = remote.get(remote_id)
            if item is None or item.get("deleted"):
                result[remote_id] = None
                continue
            remote_etag = item.get("etag") if isinstance(item.get("etag"), str) else None
            if etag is not None and remote_etag == etag:
                continue
            status = TodoStatus.DONE if item.get("status") == "completed" else TodoStatus.OPEN
            result[remote_id] = RemoteTask(
                remote_id=remote_id,
                etag=remote_etag,
                status=status,
                modified_at=_parse_time(item.get("updated")),
            )
        return result
