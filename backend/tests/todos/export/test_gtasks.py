"""Contract tests of the Google Tasks sink against a mocked Tasks API v1 (respx).

Answers follow the Tasks API documentation; titles, IDs and tokens are invented."""

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
import respx

from app.core.config import GmailSettings
from app.mail.providers.gmail_auth import TOKEN_URL
from app.todos.export.base import (
    SinkAuthError,
    SinkConflictError,
    SinkError,
    SinkNotFoundError,
    SinkUnavailableError,
    TaskData,
)
from app.todos.export.gtasks import (
    MAX_NOTES,
    TASKS_API,
    GoogleTasksSink,
    build_notes,
    task_body,
    uid_of,
)
from app.todos.models import TodoPriority, TodoStatus

LIST = "MDEyMzQ1Njc4OTAxMjM0NTY3ODk6MDow"
TASKS = f"{TASKS_API}/lists/{LIST}/tasks"
UID = "6f1c1e9e-0d7c-4d63-9b55-6b0d3c0c1a11"
MAIL_URL = "https://ollamail.example.org/inbox?message=0b9d"


class FakeTokens:
    def __init__(self) -> None:
        self.refreshed = 0

    async def token(self, *, refresh: bool = False) -> str:
        self.refreshed += refresh
        return f"access-{self.refreshed}"


def task(**changes: Any) -> TaskData:
    values: dict[str, Any] = {
        "uid": UID,
        "title": "Angebot prüfen",
        "description": "Bis Freitag an das Team.",
        "due_date": date(2026, 10, 9),
        "priority": TodoPriority.HIGH,
        "status": TodoStatus.OPEN,
        "completed_at": None,
        "modified_at": datetime(2026, 10, 2, 9, 30, tzinfo=UTC),
        "url": MAIL_URL,
    }
    return TaskData(**{**values, **changes})


def remote(task_id: str, etag: str, **changes: Any) -> dict[str, Any]:
    return {
        "kind": "tasks#task",
        "id": task_id,
        "etag": etag,
        "title": "Angebot prüfen",
        "updated": "2026-10-02T10:00:00.000Z",
        "status": "needsAction",
        **changes,
    }


@pytest.fixture
def tokens() -> FakeTokens:
    return FakeTokens()


@pytest.fixture
def google() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
async def sink(tokens: FakeTokens) -> Any:
    sink = GoogleTasksSink("rt-1", GmailSettings(), timeout=5, tokens=tokens)
    yield sink
    await sink.aclose()


def sent(route: respx.Route, index: int = -1) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(route.calls[index].request.content)
    return body


def test_task_body_maps_fields() -> None:
    body = task_body(task())

    assert body["title"] == "Angebot prüfen"
    assert body["due"] == "2026-10-09T00:00:00.000Z"
    assert body["status"] == "needsAction"
    assert body["completed"] is None
    assert body["notes"] == f"Bis Freitag an das Team.\n\n{MAIL_URL}\n[ollamail:{UID}]"
    assert uid_of(body["notes"]) == UID
    # Google Tasks has no priority.
    assert "priority" not in body


def test_task_body_done_and_dismissed() -> None:
    finished = datetime(2026, 10, 3, 8, 0, tzinfo=UTC)
    done = task_body(task(status=TodoStatus.DONE, completed_at=finished, due_date=None))
    dismissed = task_body(task(status=TodoStatus.DISMISSED))

    assert (done["status"], done["completed"], done["due"]) == (
        "completed",
        "2026-10-03T08:00:00.000Z",
        None,
    )
    # No "cancelled" in Google Tasks: dismissed counts as completed.
    assert dismissed["status"] == "completed"


def test_long_description_is_cut_but_keeps_link_and_marker() -> None:
    notes = build_notes(task(description="x" * 20_000))

    assert len(notes) <= MAX_NOTES
    assert notes.endswith(f"{MAIL_URL}\n[ollamail:{UID}]")
    assert build_notes(task(description=None, url=None)) == f"[ollamail:{UID}]"


async def test_list_task_lists_pages(sink: GoogleTasksSink, google: respx.MockRouter) -> None:
    route = google.get(f"{TASKS_API}/users/@me/lists").mock(
        side_effect=[
            httpx.Response(
                200,
                json={"items": [{"id": "a", "title": "Meine Aufgaben"}], "nextPageToken": "p2"},
            ),
            httpx.Response(200, json={"items": [{"id": "b", "title": "Arbeit"}]}),
        ]
    )

    lists = await sink.list_task_lists()

    assert [(item.id, item.name) for item in lists] == [("a", "Meine Aufgaben"), ("b", "Arbeit")]
    assert route.calls[1].request.url.params["pageToken"] == "p2"
    assert route.calls[0].request.headers["Authorization"] == "Bearer access-0"


async def test_push_creates_task(sink: GoogleTasksSink, google: respx.MockRouter) -> None:
    google.get(TASKS).mock(return_value=httpx.Response(200, json={"items": []}))
    create = google.post(TASKS).mock(return_value=httpx.Response(200, json=remote("t1", '"e1"')))

    version = await sink.push(LIST, task())

    assert (version.remote_id, version.etag) == ("t1", '"e1"')
    assert sent(create)["notes"].endswith(f"[ollamail:{UID}]")
    listing = google.calls[0].request.url.params
    assert (listing["showDeleted"], listing["showHidden"], listing["showCompleted"]) == (
        "true",
        "true",
        "true",
    )


async def test_push_again_overwrites_instead_of_duplicating(
    sink: GoogleTasksSink, google: respx.MockRouter
) -> None:
    existing = remote("t1", '"e1"', notes=f"old\n\n[ollamail:{UID}]")
    other = remote("t2", '"e2"', notes="written in Google Tasks")
    deleted = remote("t3", '"e3"', notes="[ollamail:other-uid]", deleted=True)
    google.get(TASKS).mock(
        return_value=httpx.Response(200, json={"items": [existing, other, deleted]})
    )
    create = google.post(TASKS).mock(return_value=httpx.Response(200, json=remote("t5", '"e5"')))
    patch = google.patch(f"{TASKS}/t1").mock(
        return_value=httpx.Response(200, json=remote("t1", '"e4"'))
    )

    version = await sink.push(LIST, task())
    await sink.push(LIST, task(uid="other-uid"))  # deleted there: created anew

    assert version == type(version)("t1", '"e4"')
    assert sent(patch)["title"] == "Angebot prüfen"
    assert create.call_count == 1
    # One listing per sync run.
    assert sum(1 for call in google.calls if call.request.method == "GET") == 1


async def test_create_is_not_retried(sink: GoogleTasksSink, google: respx.MockRouter) -> None:
    google.get(TASKS).mock(return_value=httpx.Response(200, json={}))
    create = google.post(TASKS).mock(return_value=httpx.Response(503))

    with pytest.raises(SinkUnavailableError):
        await sink.push(LIST, task())
    assert create.call_count == 1


async def test_update_sends_if_match(sink: GoogleTasksSink, google: respx.MockRouter) -> None:
    patch = google.patch(f"{TASKS}/t1").mock(
        return_value=httpx.Response(200, json=remote("t1", '"e2"', status="completed"))
    )

    version = await sink.complete(LIST, "t1", '"e1"', task(status=TodoStatus.DONE))

    assert version.etag == '"e2"'
    assert patch.calls[0].request.headers["If-Match"] == '"e1"'
    assert sent(patch)["status"] == "completed"


@pytest.mark.parametrize(
    ("status", "error"),
    [(412, SinkConflictError), (404, SinkNotFoundError), (400, SinkError)],
)
async def test_update_errors(
    sink: GoogleTasksSink, google: respx.MockRouter, status: int, error: type[SinkError]
) -> None:
    google.patch(f"{TASKS}/t1").mock(
        return_value=httpx.Response(status, json={"error": {"message": "secret task title"}})
    )

    with pytest.raises(error) as raised:
        await sink.update(LIST, "t1", '"e1"', task())
    # Never Google's texts.
    assert "secret" not in str(raised.value)


async def test_update_of_a_deleted_task(sink: GoogleTasksSink, google: respx.MockRouter) -> None:
    google.patch(f"{TASKS}/t1").mock(
        return_value=httpx.Response(200, json=remote("t1", '"e2"', deleted=True))
    )

    with pytest.raises(SinkNotFoundError):
        await sink.update(LIST, "t1", None, task())


async def test_delete_ignores_missing_tasks(
    sink: GoogleTasksSink, google: respx.MockRouter
) -> None:
    route = google.delete(f"{TASKS}/t1").mock(
        side_effect=[httpx.Response(204), httpx.Response(404)]
    )

    await sink.delete(LIST, "t1")
    await sink.delete(LIST, "t1")

    assert route.call_count == 2


async def test_changes(sink: GoogleTasksSink, google: respx.MockRouter) -> None:
    google.get(TASKS).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "items": [
                        remote("same", '"s1"'),
                        remote(
                            "done", '"d2"', status="completed", updated="2026-10-03T07:00:00.000Z"
                        ),
                    ],
                    "nextPageToken": "p2",
                },
            ),
            httpx.Response(200, json={"items": [remote("gone", '"g2"', deleted=True)]}),
        ]
    )

    changes = await sink.changes(
        LIST, {"same": '"s1"', "done": '"d1"', "gone": '"g1"', "purged": '"p1"'}
    )

    assert set(changes) == {"done", "gone", "purged"}
    assert changes["gone"] is None and changes["purged"] is None
    done = changes["done"]
    assert done is not None
    assert (done.status, done.etag) == (TodoStatus.DONE, '"d2"')
    assert done.modified_at == datetime(2026, 10, 3, 7, 0, tzinfo=UTC)


async def test_changes_without_known_tasks_sends_nothing(
    sink: GoogleTasksSink, google: respx.MockRouter
) -> None:
    assert await sink.changes(LIST, {}) == {}
    assert not google.calls


async def test_missing_list_is_not_found(sink: GoogleTasksSink, google: respx.MockRouter) -> None:
    google.get(TASKS).mock(return_value=httpx.Response(404))

    with pytest.raises(SinkNotFoundError):
        await sink.changes(LIST, {"t1": None})


async def test_401_refreshes_once_then_fails(
    sink: GoogleTasksSink, google: respx.MockRouter, tokens: FakeTokens
) -> None:
    route = google.get(f"{TASKS_API}/users/@me/lists").mock(return_value=httpx.Response(401))

    with pytest.raises(SinkAuthError):
        await sink.list_task_lists()

    assert tokens.refreshed == 1
    assert route.call_count == 2
    assert route.calls[1].request.headers["Authorization"] == "Bearer access-1"


async def test_429_is_retried(sink: GoogleTasksSink, google: respx.MockRouter) -> None:
    route = google.get(f"{TASKS_API}/users/@me/lists").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json={"items": [{"id": "a", "title": "Meine Aufgaben"}]}),
        ]
    )

    assert [item.id for item in await sink.list_task_lists()] == ["a"]
    assert route.call_count == 2


async def test_rate_limit_that_persists_is_unavailable(google: respx.MockRouter) -> None:
    async def no_sleep(_: float) -> None:
        return None

    sink = GoogleTasksSink("rt-1", GmailSettings(), timeout=5, tokens=FakeTokens())
    route = google.get(f"{TASKS_API}/users/@me/lists").mock(return_value=httpx.Response(429))
    api = sink._api()
    api._sleep = no_sleep

    with pytest.raises(SinkUnavailableError):
        await sink.list_task_lists()
    assert route.call_count == 5
    await sink.aclose()


async def test_revoked_refresh_token_needs_reconnect(google: respx.MockRouter) -> None:
    google.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
    sink = GoogleTasksSink(
        "rt-revoked",
        GmailSettings(client_id="client", client_secret="secret"),
        timeout=5,
    )

    with pytest.raises(SinkAuthError):
        await sink.list_task_lists()
    await sink.aclose()


async def test_oauth_not_configured(google: respx.MockRouter) -> None:
    sink = GoogleTasksSink("rt-1", GmailSettings(), timeout=5)

    with pytest.raises(SinkError) as raised:
        await sink.list_task_lists()
    assert raised.value.code == "oauth_not_configured"
    await sink.aclose()


async def test_without_refresh_token(google: respx.MockRouter) -> None:
    sink = GoogleTasksSink("", GmailSettings(), timeout=5)

    with pytest.raises(SinkAuthError):
        await sink.list_task_lists()
    await sink.aclose()


@pytest.mark.db
async def test_sync_round_trip(db_session: Any, google: respx.MockRouter) -> None:
    """The real sync (``service.sync_target``) with this sink: export, then "done" in Google
    Tasks comes back."""
    from app.core.config import TodosSettings
    from app.todos.export import service
    from app.todos.export.refs import RefState, read_ref
    from app.todos.models import Todo
    from tests.factories import make_user
    from tests.todos.export.conftest import make_target

    user = await make_user(db_session, email="erika@example.org")
    target = await make_target(db_session, user, list_id=LIST)
    target.sink = "gtasks"
    target.config = {"refresh_token": "rt-1"}
    todo = Todo(user_id=user.id, title="Angebot prüfen", due_date=date(2026, 10, 9))
    db_session.add(todo)
    await db_session.commit()

    def build(*_: Any) -> GoogleTasksSink:
        # A new sink per run, like ``registry.create_sink``.
        return GoogleTasksSink("rt-1", GmailSettings(), timeout=5, tokens=FakeTokens())

    settings = TodosSettings(export_sinks=["gtasks"])
    listing = google.get(TASKS).mock(return_value=httpx.Response(200, json={"items": []}))
    create = google.post(TASKS).mock(return_value=httpx.Response(200, json=remote("t1", '"e1"')))

    first = await service.sync_target(db_session, target.id, settings=settings, sink_builder=build)

    assert first is not None and first.created == 1 and first.error is None
    assert sent(create)["due"] == "2026-10-09T00:00:00.000Z"
    await db_session.refresh(todo)
    ref = read_ref(todo.external_refs, "gtasks")
    assert ref is not None and (ref.id, ref.etag, ref.state) == ("t1", '"e1"', RefState.SYNCED)

    listing.mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    remote("t1", '"e2"', status="completed", updated="2099-01-01T00:00:00.000Z")
                ]
            },
        )
    )
    second = await service.sync_target(
        db_session, target.id, settings=settings, sink_builder=build, force_poll=True
    )

    assert second is not None and second.pulled == 1
    await db_session.refresh(todo)
    assert todo.status == TodoStatus.DONE
