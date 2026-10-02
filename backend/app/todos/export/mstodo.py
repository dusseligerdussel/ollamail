"""Microsoft To Do sink (Graph): todos as tasks in a To Do list
(docs/providers/microsoft365.md §11).

Auth and HTTP come from the Microsoft 365 mail provider: ``DelegatedTokens`` refreshes the
access token (``Tasks.ReadWrite``, delegated; To Do has no app-only access) and
``GraphClient`` retries throttled requests after ``Retry-After``. The account is connected
with its own OAuth flow (``app.todos.export.mstodo_router``); its tokens live encrypted in
``todo_export_targets.config``::

    {"username": <UPN>, "account_id": <Entra object ID>, "refresh_token": ..., "access_token": ...,
     "expires_at": ..., "delta": {"list": ..., "link": ..., "pending": {...}}}

* **Writes:** ``POST``/``PATCH``/``DELETE /me/todo/lists/{list}/tasks/{task}``, updates with
  ``If-Match`` (412 = conflict). A task carries a ``linkedResource`` (application
  ``ollamail``, ``externalId`` = todo ID, link to the mail). Graph has no client-chosen IDs,
  so ``push`` first looks for a task with the same ``externalId`` (once per sync run) and
  overwrites it instead of creating a duplicate.
* **Mapping:** status ``notStarted``/``completed`` (dismissed todos are ``completed``, To Do
  has no "cancelled"), ``importance`` for the priority, ``dueDateTime`` at noon UTC (To Do
  shows the date in the user's time zone; noon keeps it on the same day almost
  everywhere), ``body`` with the description and the link to the mail.
* **Status sync** (``changes``): delta query on the list. The delta link and the remote
  changes not yet taken over (the sync may skip a todo the user changed meanwhile) are
  kept in the config, so no change is lost between runs; an expired delta link starts a
  full round. A handful of tasks (e.g. after a conflict) are read one by one instead.

Errors are mapped to ``SinkError`` codes; Graph answers are never logged or stored.
"""

import asyncio
import contextlib
import re
import time
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from typing import Any, ClassVar
from urllib.parse import quote

import httpx

from app.core.config import GraphSettings
from app.mail.providers.base import (
    AuthenticationError,
    ConfigurationError,
    ConnectionFailedError,
    CursorInvalidError,
    ProviderError,
)
from app.mail.providers.graph_auth import Clock, DelegatedTokens, resource
from app.mail.providers.graph_client import GraphClient, GraphNotFoundError, Sleep
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
from app.todos.models import TodoPriority, TodoStatus

APPLICATION_NAME = "ollamail"
# Up to this many known tasks are read one by one instead of with a delta round.
DIRECT_LIMIT = 5
_SCOPES = ("User.Read", "Tasks.ReadWrite")
_DUE_TIME = "12:00:00"
_STATUS = {
    TodoStatus.OPEN: "notStarted",
    TodoStatus.DONE: "completed",
    TodoStatus.DISMISSED: "completed",
}
_IMPORTANCE = {
    TodoPriority.HIGH: "high",
    TodoPriority.NORMAL: "normal",
    TodoPriority.LOW: "low",
}
# Graph returns 7 fractional digits; ``fromisoformat`` takes at most 6.
_FRACTION = re.compile(r"(\.\d{6})\d+")


def todo_scopes(settings: GraphSettings) -> list[str]:
    """Delegated scopes for Microsoft To Do, requested by the connect flow."""
    return ["offline_access", *(f"{resource(settings)}/{name}" for name in _SCOPES)]


@contextlib.contextmanager
def _mapped_errors(*, delta: bool = False) -> Iterator[None]:
    try:
        yield
    except CursorInvalidError:
        if delta:
            raise
        raise SinkError("request_failed") from None
    except AuthenticationError:
        raise SinkAuthError() from None
    except ConfigurationError:
        raise SinkError("not_configured") from None
    except ConnectionFailedError:
        raise SinkUnavailableError() from None
    except GraphNotFoundError:
        raise SinkNotFoundError() from None
    except ProviderError as exc:
        if exc.code == "precondition_failed":
            raise SinkConflictError() from None
        raise SinkError(exc.code) from None


def _segment(value: str) -> str:
    if not value:
        raise SinkNotFoundError()
    return quote(value, safe="")


def _utc(value: datetime) -> dict[str, str]:
    stamp = value.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="seconds")
    return {"dateTime": stamp, "timeZone": "UTC"}


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(_FRACTION.sub(r"\1", value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def task_body(task: TaskData, *, create: bool) -> dict[str, Any]:
    """The ``todoTask`` sent for ``task``."""
    content = "\n\n".join(part for part in (task.description, task.url) if part)
    body: dict[str, Any] = {
        "title": task.title,
        "body": {"contentType": "text", "content": content},
        "importance": _IMPORTANCE[task.priority],
        "status": _STATUS[task.status],
    }
    if task.due_date is not None:
        body["dueDateTime"] = {
            "dateTime": f"{task.due_date.isoformat()}T{_DUE_TIME}",
            "timeZone": "UTC",
        }
    elif not create:
        body["dueDateTime"] = None
    if task.status != TodoStatus.OPEN:
        body["completedDateTime"] = _utc(task.completed_at or task.modified_at)
    if create:
        link: dict[str, str] = {
            "applicationName": APPLICATION_NAME,
            "displayName": APPLICATION_NAME,
            "externalId": task.uid,
        }
        if task.url:
            link["webUrl"] = task.url
        body["linkedResources"] = [link]
    return body


def _remote_entry(item: Mapping[str, Any]) -> dict[str, Any]:
    """What the status sync keeps of a remote task (no title or other content)."""
    etag = item.get("@odata.etag")
    modified = _parse_time(item.get("lastModifiedDateTime"))
    return {
        "etag": etag if isinstance(etag, str) else None,
        "status": "completed" if item.get("status") == "completed" else "open",
        "modified": modified.isoformat() if modified else None,
    }


def _remote_task(remote_id: str, entry: Mapping[str, Any]) -> RemoteTask:
    return RemoteTask(
        remote_id=remote_id,
        etag=entry.get("etag"),
        status=TodoStatus.DONE if entry.get("status") == "completed" else TodoStatus.OPEN,
        modified_at=_parse_time(entry.get("modified")),
    )


class GraphTodoSink(TodoSink):
    kind: ClassVar[str] = "mstodo"

    def __init__(
        self,
        settings: GraphSettings,
        config: Mapping[str, Any],
        *,
        timeout: float,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = time.time,
    ) -> None:
        self._config = dict(config)
        self._changed = False
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport)
        tokens = DelegatedTokens(
            settings,
            self._http,
            self._config,
            scopes=todo_scopes(settings),
            save=self._save_tokens,
            clock=clock,
        )
        self._client = GraphClient(
            api_url=settings.api_url,
            token=tokens,
            timeout=timeout,
            max_retries=settings.max_retries,
            transport=transport,
            sleep=sleep,
        )
        # Tasks created by ollamail per list: ``externalId`` → version (``push``).
        self._ours: dict[str, dict[str, RemoteVersion]] = {}

    async def _save_tokens(self, credentials: dict[str, Any]) -> None:
        self._config.update(credentials)
        self._changed = True

    def updated_config(self) -> Mapping[str, Any] | None:
        return dict(self._config) if self._changed else None

    async def aclose(self) -> None:
        await self._client.aclose()
        await self._http.aclose()

    # -- requests ---------------------------------------------------------------------------

    def _tasks(self, list_id: str) -> str:
        return f"/me/todo/lists/{_segment(list_id)}/tasks"

    def _task(self, list_id: str, remote_id: str) -> str:
        return f"{self._tasks(list_id)}/{_segment(remote_id)}"

    async def _json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        with _mapped_errors():
            response = await self._client.request(
                method, path, params=params, json_body=json_body, headers=headers
            )
        try:
            data = response.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise SinkError("invalid_response")
        return data

    async def _all(
        self, path: str, params: Mapping[str, str] | None = None
    ) -> list[dict[str, Any]]:
        with _mapped_errors():
            return [item async for item in self._client.items(path, params=params)]

    @staticmethod
    def _version(data: Mapping[str, Any]) -> RemoteVersion:
        remote_id = data.get("id")
        etag = data.get("@odata.etag")
        if not isinstance(remote_id, str) or not remote_id:
            raise SinkError("invalid_response")
        return RemoteVersion(remote_id, etag if isinstance(etag, str) else None)

    def _written(self, data: Mapping[str, Any]) -> RemoteVersion:
        """Version of a task we just wrote; it supersedes a remote change still pending
        from the status sync (``_changes_delta``)."""
        version = self._version(data)
        state = self._config.get("delta")
        if not isinstance(state, dict):
            return version
        pending = state.get("pending")
        if isinstance(pending, dict) and version.remote_id in pending:
            state["pending"] = {k: v for k, v in pending.items() if k != version.remote_id}
            self._changed = True
        return version

    # -- TodoSink ---------------------------------------------------------------------------

    async def list_task_lists(self) -> list[TaskList]:
        found: list[tuple[bool, TaskList]] = []
        for item in await self._all("/me/todo/lists"):
            list_id, name = item.get("id"), item.get("displayName")
            if isinstance(list_id, str) and list_id and isinstance(name, str):
                default = item.get("wellknownListName") == "defaultList"
                found.append((default, TaskList(id=list_id, name=name)))
        # The default list ("Tasks") first, then by name.
        found.sort(key=lambda pair: (not pair[0], pair[1].name.casefold()))
        return [task_list for _, task_list in found]

    async def _our_tasks(self, list_id: str) -> dict[str, RemoteVersion]:
        """Tasks of the list created by ollamail, by todo ID (one scan per sync run)."""
        if list_id not in self._ours:
            ours: dict[str, RemoteVersion] = {}
            items = await self._all(
                self._tasks(list_id), {"$select": "id", "$expand": "linkedResources"}
            )
            for item in items:
                links = item.get("linkedResources")
                for link in links if isinstance(links, list) else ():
                    if (
                        isinstance(link, dict)
                        and link.get("applicationName") == APPLICATION_NAME
                        and isinstance(link.get("externalId"), str)
                    ):
                        with contextlib.suppress(SinkError):
                            ours[link["externalId"]] = self._version(item)
            self._ours[list_id] = ours
        return self._ours[list_id]

    async def push(self, list_id: str, task: TaskData) -> RemoteVersion:
        ours = await self._our_tasks(list_id)
        existing = ours.get(task.uid)
        if existing is not None:
            # Created before, but the answer got lost: overwrite, do not duplicate.
            with contextlib.suppress(SinkNotFoundError):
                version = await self.update(list_id, existing.remote_id, None, task)
                ours[task.uid] = version
                return version
        data = await self._json(
            "POST", self._tasks(list_id), json_body=task_body(task, create=True)
        )
        version = self._written(data)
        ours[task.uid] = version
        return version

    async def update(
        self, list_id: str, remote_id: str, etag: str | None, task: TaskData
    ) -> RemoteVersion:
        data = await self._json(
            "PATCH",
            self._task(list_id, remote_id),
            json_body=task_body(task, create=False),
            headers={"If-Match": etag} if etag else None,
        )
        return self._written(data)

    async def delete(self, list_id: str, remote_id: str) -> None:
        with contextlib.suppress(SinkNotFoundError), _mapped_errors():
            await self._client.request("DELETE", self._task(list_id, remote_id))

    async def changes(
        self, list_id: str, known: Mapping[str, str | None]
    ) -> dict[str, RemoteTask | None]:
        if not known:
            return {}
        if len(known) <= DIRECT_LIMIT:
            return await self._changes_direct(list_id, known)
        return await self._changes_delta(list_id, known)

    async def _changes_direct(
        self, list_id: str, known: Mapping[str, str | None]
    ) -> dict[str, RemoteTask | None]:
        changed: dict[str, RemoteTask | None] = {}
        for remote_id, etag in known.items():
            try:
                item = await self._json(
                    "GET",
                    self._task(list_id, remote_id),
                    params={"$select": "id,status,lastModifiedDateTime"},
                )
            except SinkNotFoundError:
                changed[remote_id] = None
                continue
            entry = _remote_entry(item)
            if entry["etag"] is None or entry["etag"] != etag:
                changed[remote_id] = _remote_task(remote_id, entry)
        return changed

    async def _delta_round(self, start: str) -> tuple[dict[str, dict[str, Any]], set[str], str]:
        """All pages of one delta round: changed tasks, removed IDs, the next delta link."""
        seen: dict[str, dict[str, Any]] = {}
        removed: set[str] = set()
        url = start
        while True:
            with _mapped_errors(delta=True):
                page = await self._client.get_json(url)
            for item in page.get("value", []):
                remote_id = item.get("id") if isinstance(item, dict) else None
                if not isinstance(remote_id, str):
                    continue
                if "@removed" in item:
                    removed.add(remote_id)
                    seen.pop(remote_id, None)
                else:
                    seen[remote_id] = _remote_entry(item)
                    removed.discard(remote_id)
            next_link = page.get("@odata.nextLink")
            if isinstance(next_link, str):
                url = next_link
                continue
            delta_link = page.get("@odata.deltaLink")
            if not isinstance(delta_link, str):
                raise SinkError("invalid_response")
            return seen, removed, delta_link

    async def _changes_delta(
        self, list_id: str, known: Mapping[str, str | None]
    ) -> dict[str, RemoteTask | None]:
        state = self._config.get("delta")
        link: str | None = None
        pending: dict[str, Any] = {}
        if isinstance(state, dict) and state.get("list") == list_id:
            link = state.get("link") if isinstance(state.get("link"), str) else None
            if link and isinstance(state.get("pending"), dict):
                pending = dict(state["pending"])
        full = link is None
        try:
            seen, removed, next_link = await self._delta_round(
                link or f"{self._tasks(list_id)}/delta"
            )
        except CursorInvalidError:
            # Delta link expired: start over with a full round.
            full, pending = True, {}
            try:
                seen, removed, next_link = await self._delta_round(f"{self._tasks(list_id)}/delta")
            except CursorInvalidError:
                raise SinkError("invalid_response") from None
        pending.update(seen)
        pending.update(dict.fromkeys(removed))
        if full:
            # A full round lists every task: what is missing was deleted.
            pending.update({remote_id: None for remote_id in known if remote_id not in seen})

        changed: dict[str, RemoteTask | None] = {}
        unacknowledged: dict[str, Any] = {}
        for remote_id, etag in known.items():
            if remote_id not in pending:
                continue
            entry = pending[remote_id]
            if entry is None:
                changed[remote_id] = None
            elif entry.get("etag") is None or entry.get("etag") != etag:
                changed[remote_id] = _remote_task(remote_id, entry)
            else:
                continue
            # Reported again next time until the sync has stored it (etag matches).
            unacknowledged[remote_id] = entry
        self._config["delta"] = {"list": list_id, "link": next_link, "pending": unacknowledged}
        self._changed = True
        return changed
