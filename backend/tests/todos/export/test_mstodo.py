"""Microsoft To Do sink against a mocked Graph API (``respx``); synthetic data only."""

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
import respx

from app.core.config import GraphSettings
from app.todos.export.base import (
    RemoteTask,
    SinkAuthError,
    SinkConflictError,
    SinkError,
    SinkNotFoundError,
    SinkUnavailableError,
    TaskData,
)
from app.todos.export.mstodo import DIRECT_LIMIT, GraphTodoSink, task_body, todo_scopes
from app.todos.models import TodoPriority, TodoStatus
from tests.mail.graph_helpers import (
    GRAPH,
    NOW,
    TOKEN_URL,
    Sleeps,
    form,
    graph_settings,
    token_response,
)

LIST = "AQMkADAwATM0MDAAMS1-list"
TASKS = f"{GRAPH}/me/todo/lists/{LIST}/tasks"
DELTA = f"{TASKS}/delta"
MODIFIED = datetime(2026, 10, 7, 9, 30, tzinfo=UTC)


def config(**changes: Any) -> dict[str, Any]:
    return {
        "username": "erika@example.com",
        "account_id": "graph-user-1",
        "access_token": "access-1",
        "refresh_token": "refresh-1",
        "expires_at": int(NOW + 3600),
        **changes,
    }


def task(uid: str | None = None, **changes: object) -> TaskData:
    values: dict[str, object] = {
        "uid": uid or str(uuid.uuid4()),
        "title": "Send the quarterly report",
        "description": "Numbers for Q3, see mail",
        "due_date": date(2026, 10, 9),
        "priority": TodoPriority.HIGH,
        "status": TodoStatus.OPEN,
        "completed_at": None,
        "modified_at": MODIFIED,
        "url": "https://mail.example.org/inbox?message=0192",
    }
    values.update(changes)
    return TaskData(**values)  # type: ignore[arg-type]


def remote(task_id: str, etag: str, status: str = "notStarted", **extra: Any) -> dict[str, Any]:
    return {
        "@odata.etag": etag,
        "id": task_id,
        "status": status,
        "lastModifiedDateTime": "2026-10-08T07:15:00.1234567Z",
        **extra,
    }


def make_sink(
    graph: GraphSettings | None = None, sleep: Sleeps | None = None, **values: Any
) -> GraphTodoSink:
    return GraphTodoSink(
        graph or graph_settings(max_retries=2),
        config(**values),
        timeout=5,
        sleep=Sleeps() if sleep is None else sleep,
        clock=lambda: NOW,
    )


@pytest.fixture
async def sink() -> AsyncIterator[GraphTodoSink]:
    sink = make_sink()
    yield sink
    await sink.aclose()


def body(request: httpx.Request) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(request.content)
    return data


def test_scopes_ask_for_tasks_only() -> None:
    assert todo_scopes(graph_settings()) == [
        "offline_access",
        "https://graph.microsoft.com/User.Read",
        "https://graph.microsoft.com/Tasks.ReadWrite",
    ]


def test_task_body_maps_the_fields() -> None:
    data = task("todo-1")

    created = task_body(data, create=True)

    assert created == {
        "title": "Send the quarterly report",
        "body": {
            "contentType": "text",
            "content": "Numbers for Q3, see mail\n\nhttps://mail.example.org/inbox?message=0192",
        },
        "importance": "high",
        "status": "notStarted",
        "dueDateTime": {"dateTime": "2026-10-09T12:00:00", "timeZone": "UTC"},
        "linkedResources": [
            {
                "applicationName": "ollamail",
                "displayName": "ollamail",
                "externalId": "todo-1",
                "webUrl": "https://mail.example.org/inbox?message=0192",
            }
        ],
    }


def test_task_body_for_updates_clears_the_due_date_and_completes() -> None:
    done = task(
        "todo-1",
        due_date=None,
        description=None,
        url=None,
        priority=TodoPriority.LOW,
        status=TodoStatus.DONE,
        completed_at=datetime(2026, 10, 8, 14, 0, tzinfo=UTC),
    )

    updated = task_body(done, create=False)

    assert updated["dueDateTime"] is None
    assert updated["status"] == "completed"
    assert updated["importance"] == "low"
    assert updated["body"] == {"contentType": "text", "content": ""}
    assert updated["completedDateTime"] == {"dateTime": "2026-10-08T14:00:00", "timeZone": "UTC"}
    assert "linkedResources" not in updated
    # To Do has no "cancelled": dismissed todos are done there.
    assert task_body(task(status=TodoStatus.DISMISSED), create=False)["status"] == "completed"


@respx.mock
async def test_lists_default_list_first_and_follows_pages(sink: GraphTodoSink) -> None:
    respx.get(f"{GRAPH}/me/todo/lists").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "list-b", "displayName": "Work", "wellknownListName": "none"},
                    ],
                    "@odata.nextLink": f"{GRAPH}/me/todo/lists?$skiptoken=2",
                },
            ),
            httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "list-a", "displayName": "Errands", "wellknownListName": "none"},
                        {"id": LIST, "displayName": "Tasks", "wellknownListName": "defaultList"},
                        {"id": "broken"},
                    ]
                },
            ),
        ]
    )

    lists = await sink.list_task_lists()

    assert [(item.id, item.name) for item in lists] == [
        (LIST, "Tasks"),
        ("list-a", "Errands"),
        ("list-b", "Work"),
    ]
    assert sink.updated_config() is None


@respx.mock
async def test_push_creates_the_task_with_a_link(sink: GraphTodoSink) -> None:
    scan = respx.get(TASKS).respond(json={"value": []})
    create = respx.post(TASKS).respond(201, json=remote("task-1", 'W/"e1"'))
    data = task("todo-1")

    version = await sink.push(LIST, data)
    second = await sink.push(LIST, task("todo-2"))

    assert (version.remote_id, version.etag) == ("task-1", 'W/"e1"')
    assert second.remote_id == "task-1"
    # One scan for tasks created earlier per run, not one per push.
    assert scan.call_count == 1
    assert scan.calls[0].request.url.params["$expand"] == "linkedResources"
    sent = body(create.calls[0].request)
    assert sent["title"] == "Send the quarterly report"
    assert sent["linkedResources"][0]["externalId"] == "todo-1"
    assert create.calls[0].request.headers["Authorization"] == "Bearer access-1"


@respx.mock
async def test_pushing_again_overwrites_instead_of_duplicating(sink: GraphTodoSink) -> None:
    respx.get(TASKS).respond(
        json={
            "value": [
                {
                    "@odata.etag": 'W/"e1"',
                    "id": "task-1",
                    "linkedResources": [{"applicationName": "ollamail", "externalId": "todo-1"}],
                },
                {
                    "@odata.etag": 'W/"x"',
                    "id": "task-foreign",
                    "linkedResources": [{"applicationName": "Outlook", "externalId": "todo-2"}],
                },
            ]
        }
    )
    create = respx.post(TASKS).respond(201, json=remote("task-2", 'W/"n1"'))
    patch = respx.patch(f"{TASKS}/task-1").respond(json=remote("task-1", 'W/"e2"'))

    version = await sink.push(LIST, task("todo-1", title="Renamed"))
    other = await sink.push(LIST, task("todo-2"))

    assert (version.remote_id, version.etag) == ("task-1", 'W/"e2"')
    assert body(patch.calls[0].request)["title"] == "Renamed"
    assert "If-Match" not in patch.calls[0].request.headers
    # Only tasks linked by ollamail count.
    assert other.remote_id == "task-2"
    assert create.call_count == 1


@respx.mock
async def test_update_sends_if_match_and_maps_412_to_a_conflict(sink: GraphTodoSink) -> None:
    patch = respx.patch(f"{TASKS}/task-1").mock(
        side_effect=[
            httpx.Response(200, json=remote("task-1", 'W/"e2"')),
            httpx.Response(412, json={"error": {"code": "PreconditionFailed", "message": "x"}}),
        ]
    )

    version = await sink.update(LIST, "task-1", 'W/"e1"', task("todo-1"))
    with pytest.raises(SinkConflictError):
        await sink.update(LIST, "task-1", 'W/"e1"', task("todo-1"))

    assert version.etag == 'W/"e2"'
    assert patch.calls[0].request.headers["If-Match"] == 'W/"e1"'


@respx.mock
async def test_update_of_a_deleted_task_is_not_found(sink: GraphTodoSink) -> None:
    respx.patch(f"{TASKS}/task-1").respond(
        404, json={"error": {"code": "ErrorItemNotFound", "message": "Subject: secret"}}
    )

    with pytest.raises(SinkNotFoundError) as caught:
        await sink.update(LIST, "task-1", None, task())

    # Graph messages never end up in the error.
    assert "secret" not in str(caught.value)


@respx.mock
async def test_delete_ignores_tasks_that_are_gone(sink: GraphTodoSink) -> None:
    route = respx.delete(f"{TASKS}/task-1").mock(
        side_effect=[httpx.Response(204), httpx.Response(404, json={})]
    )

    await sink.delete(LIST, "task-1")
    await sink.delete(LIST, "task-1")

    assert route.call_count == 2


@respx.mock
async def test_few_known_tasks_are_read_one_by_one(sink: GraphTodoSink) -> None:
    respx.get(f"{TASKS}/task-1").respond(json=remote("task-1", 'W/"e1"'))
    respx.get(f"{TASKS}/task-2").respond(json=remote("task-2", 'W/"e9"', "completed"))
    respx.get(f"{TASKS}/task-3").respond(404, json={"error": {"code": "ErrorItemNotFound"}})

    changed = await sink.changes(LIST, {"task-1": 'W/"e1"', "task-2": 'W/"e2"', "task-3": None})

    assert changed == {
        "task-2": RemoteTask(
            "task-2",
            'W/"e9"',
            TodoStatus.DONE,
            datetime(2026, 10, 8, 7, 15, 0, 123456, tzinfo=UTC),
        ),
        "task-3": None,
    }
    # The delta state is left alone.
    assert sink.updated_config() is None


def known(count: int, etag: str = 'W/"e1"') -> dict[str, str | None]:
    return {f"task-{number}": etag for number in range(count)}


@respx.mock
async def test_first_delta_round_finds_changed_and_deleted_tasks(sink: GraphTodoSink) -> None:
    tasks = known(DIRECT_LIMIT + 2)
    items = [remote(task_id, 'W/"e1"') for task_id in list(tasks)[1:]]
    items[0] = remote("task-1", 'W/"e2"', "completed")
    respx.get(DELTA).respond(
        json={
            "value": [*items, remote("task-foreign", 'W/"f"')],
            "@odata.deltaLink": f"{DELTA}?$deltatoken=t1",
        }
    )

    changed = await sink.changes(LIST, tasks)

    assert set(changed) == {"task-0", "task-1"}
    assert changed["task-0"] is None
    remote_task = changed["task-1"]
    assert remote_task is not None and remote_task.status == TodoStatus.DONE
    stored = sink.updated_config()
    assert stored is not None
    assert stored["delta"]["link"] == f"{DELTA}?$deltatoken=t1"
    assert stored["delta"]["list"] == LIST
    # Kept until the sync has taken them over; no titles or other content.
    assert set(stored["delta"]["pending"]) == {"task-0", "task-1"}
    assert stored["delta"]["pending"]["task-1"] == {
        "etag": 'W/"e2"',
        "status": "completed",
        "modified": "2026-10-08T07:15:00.123456+00:00",
    }


@respx.mock
async def test_next_round_uses_the_delta_link_with_removed_tasks() -> None:
    tasks = known(DIRECT_LIMIT + 2)
    sink = make_sink(
        delta={
            "list": LIST,
            "link": f"{DELTA}?$deltatoken=t1",
            "pending": {"task-3": {"etag": 'W/"e5"', "status": "open", "modified": None}},
        }
    )
    route = respx.get(DELTA).respond(
        json={
            "value": [
                {"id": "task-2", "@removed": {"reason": "deleted"}},
                remote("task-4", 'W/"e1"'),
            ],
            "@odata.deltaLink": f"{DELTA}?$deltatoken=t2",
        }
    )
    try:
        changed = await sink.changes(LIST, tasks)
    finally:
        await sink.aclose()

    assert route.calls[0].request.url.params["$deltatoken"] == "t1"
    # task-2 removed; task-3 changed in an earlier round and not taken over yet.
    assert changed["task-2"] is None
    task_3 = changed["task-3"]
    assert task_3 is not None and task_3.etag == 'W/"e5"'
    assert set(changed) == {"task-2", "task-3"}
    stored = sink.updated_config()
    assert stored is not None
    assert stored["delta"]["link"] == f"{DELTA}?$deltatoken=t2"


@respx.mock
async def test_writing_a_task_settles_its_pending_change() -> None:
    sink = make_sink(
        delta={
            "list": LIST,
            "link": f"{DELTA}?$deltatoken=t1",
            "pending": {"task-3": {"etag": 'W/"e5"', "status": "completed", "modified": None}},
        }
    )
    respx.patch(f"{TASKS}/task-3").respond(json=remote("task-3", 'W/"e6"'))
    respx.get(DELTA).respond(json={"value": [], "@odata.deltaLink": f"{DELTA}?$deltatoken=t2"})
    try:
        await sink.update(LIST, "task-3", 'W/"e5"', task())
        changed = await sink.changes(LIST, {**known(DIRECT_LIMIT + 1), "task-3": 'W/"e6"'})
    finally:
        await sink.aclose()

    assert changed == {}


@respx.mock
async def test_expired_delta_link_starts_a_full_round() -> None:
    tasks = known(DIRECT_LIMIT + 1)
    sink = make_sink(delta={"list": LIST, "link": f"{DELTA}?$deltatoken=old", "pending": {}})
    route = respx.get(DELTA).mock(
        side_effect=[
            httpx.Response(410, json={"error": {"code": "SyncStateNotFound"}}),
            httpx.Response(
                200,
                json={
                    "value": [remote(task_id, 'W/"e1"') for task_id in tasks],
                    "@odata.deltaLink": f"{DELTA}?$deltatoken=new",
                },
            ),
        ]
    )
    try:
        changed = await sink.changes(LIST, tasks)
    finally:
        await sink.aclose()

    assert changed == {}
    assert "$deltatoken" not in route.calls[1].request.url.params
    stored = sink.updated_config()
    assert stored is not None and stored["delta"]["link"].endswith("=new")


@respx.mock
async def test_delta_state_of_another_list_is_ignored() -> None:
    sink = make_sink(delta={"list": "other", "link": f"{DELTA}?$deltatoken=t1", "pending": {}})
    route = respx.get(DELTA).respond(
        json={"value": [], "@odata.deltaLink": f"{DELTA}?$deltatoken=t2"}
    )
    try:
        changed = await sink.changes(LIST, known(DIRECT_LIMIT + 1))
    finally:
        await sink.aclose()

    assert "$deltatoken" not in route.calls[0].request.url.params
    # Full round: all known tasks are gone.
    assert set(changed) == set(known(DIRECT_LIMIT + 1))


@respx.mock
async def test_expired_access_token_is_refreshed_and_saved() -> None:
    sink = make_sink(expires_at=int(NOW - 10))
    token = respx.post(TOKEN_URL).mock(return_value=token_response("access-2", "refresh-2"))
    lists = respx.get(f"{GRAPH}/me/todo/lists").respond(json={"value": []})
    try:
        await sink.list_task_lists()
    finally:
        await sink.aclose()

    sent = form(token.calls[0].request)
    assert sent["grant_type"] == "refresh_token"
    assert sent["refresh_token"] == "refresh-1"
    assert "https://graph.microsoft.com/Tasks.ReadWrite" in sent["scope"].split()
    assert lists.calls[0].request.headers["Authorization"] == "Bearer access-2"
    stored = sink.updated_config()
    assert stored is not None
    assert stored["refresh_token"] == "refresh-2"
    assert stored["access_token"] == "access-2"
    assert stored["account_id"] == "graph-user-1"


@respx.mock
async def test_401_refreshes_once_then_is_an_auth_error(sink: GraphTodoSink) -> None:
    respx.post(TOKEN_URL).mock(return_value=token_response("access-2", "refresh-2"))
    route = respx.get(f"{GRAPH}/me/todo/lists").respond(401, json={})

    with pytest.raises(SinkAuthError):
        await sink.list_task_lists()

    assert route.call_count == 2


@respx.mock
async def test_revoked_refresh_token_is_an_auth_error() -> None:
    sink = make_sink(expires_at=0)
    respx.post(TOKEN_URL).respond(400, json={"error": "invalid_grant"})
    try:
        with pytest.raises(SinkAuthError):
            await sink.list_task_lists()
    finally:
        await sink.aclose()


@respx.mock
async def test_missing_permission_is_an_auth_error(sink: GraphTodoSink) -> None:
    respx.get(f"{GRAPH}/me/todo/lists").respond(403, json={"error": {"code": "Forbidden"}})

    with pytest.raises(SinkAuthError):
        await sink.list_task_lists()


@respx.mock
async def test_throttling_waits_for_retry_after() -> None:
    sleeps = Sleeps()
    sink = make_sink(sleep=sleeps)
    respx.post(TASKS).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}, json={}),
            httpx.Response(201, json=remote("task-1", 'W/"e1"')),
        ]
    )
    respx.get(TASKS).respond(json={"value": []})
    try:
        version = await sink.push(LIST, task())
    finally:
        await sink.aclose()

    assert version.remote_id == "task-1"
    assert sleeps == [7.0]


@respx.mock
async def test_lasting_throttling_is_transient() -> None:
    sink = make_sink(sleep=Sleeps())
    respx.get(f"{GRAPH}/me/todo/lists").respond(503, json={})
    try:
        with pytest.raises(SinkUnavailableError) as caught:
            await sink.list_task_lists()
    finally:
        await sink.aclose()

    assert caught.value.transient


@respx.mock
async def test_network_error_is_transient(sink: GraphTodoSink) -> None:
    respx.get(f"{GRAPH}/me/todo/lists").mock(side_effect=httpx.ConnectError("boom"))

    with pytest.raises(SinkUnavailableError):
        await sink.list_task_lists()


async def test_without_entra_app_the_sink_is_not_configured() -> None:
    sink = make_sink(GraphSettings(), expires_at=0)
    try:
        with pytest.raises(SinkError) as caught:
            await sink.list_task_lists()
    finally:
        await sink.aclose()

    assert caught.value.code == "not_configured"
