"""Integration tests: export settings API with sign-in and PostgreSQL, the todo API hooks,
and one end-to-end run against the CalDAV test server."""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditAction
from app.audit.models import audit_events
from app.core.config import Settings, TodosSettings
from app.core.db import get_db
from app.digest.storage import DigestStorage
from app.main import create_app
from app.privacy.export import collect
from app.todos.export import service
from app.todos.export.base import SinkAuthError, SinkUnavailableError
from app.todos.export.models import TodoExportTarget
from app.todos.export.router import get_export_enqueuer, get_sink_builder
from app.todos.models import Todo, TodoStatus
from tests.auth.conftest import _cheap_hashing, login, make_local_user  # noqa: F401
from tests.conftest import api_client
from tests.todos.conftest import make_mailbox
from tests.todos.export.conftest import (
    CALDAV_USERS,
    CalDAVServer,
    FakeSink,
    builder_for,
)

pytestmark = pytest.mark.db

CONNECTION = {
    "sink": "caldav",
    "url": "https://dav.example.org/remote.php/dav",
    "username": "erika",
    "password": "app-password-1",
}


def export_settings(settings: Settings, **todos: Any) -> Settings:
    values = {"export_sinks": ["caldav"], **todos}
    return settings.model_copy(update={"todos": TodosSettings.model_validate(values)})


@pytest.fixture
def queued() -> list[uuid.UUID]:
    return []


@pytest.fixture
def sink_holder(fake_sink: FakeSink) -> dict[str, Any]:
    """The sink builder used by the API (fake by default)."""
    return {"builder": builder_for(fake_sink)}


@pytest.fixture
def app_settings(settings: Settings) -> Settings:
    return export_settings(settings)


@pytest.fixture
async def app(
    app_settings: Settings,
    db_session: AsyncSession,
    queued: list[uuid.UUID],
    sink_holder: dict[str, Any],
) -> AsyncIterator[FastAPI]:
    app = create_app(app_settings)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    async def enqueue(target_id: uuid.UUID) -> None:
        queued.append(target_id)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_export_enqueuer] = lambda: enqueue
    app.dependency_overrides[get_sink_builder] = lambda: sink_holder["builder"]
    yield app
    await app.state.database.dispose()


@pytest.fixture
async def erika(app: FastAPI, db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    await make_local_user(db_session, "erika@example.org")
    async with api_client(app) as http:
        assert (await login(http, "erika@example.org")).status_code == 200
        yield http


async def user_id(client: AsyncClient) -> uuid.UUID:
    return uuid.UUID((await client.get("/auth/me")).json()["id"])


async def connect(client: AsyncClient, **changes: Any) -> dict[str, Any]:
    response = await client.put(
        "/todo-export", json={**CONNECTION, "list_id": "list-1", "mode": "auto", **changes}
    )
    assert response.status_code == 200, response.json()
    body: dict[str, Any] = response.json()
    return body


async def test_requires_sign_in(app: FastAPI) -> None:
    async with api_client(app) as anonymous:
        assert (await anonymous.get("/todo-export")).status_code == 401
        assert (await anonymous.post("/todo-export/lists", json=CONNECTION)).status_code == 401


async def test_export_is_off_unless_the_admin_allows_it(
    settings: Settings, db_session: AsyncSession, queued: list[uuid.UUID]
) -> None:
    app = create_app(settings)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    await make_local_user(db_session, "erika@example.org")
    async with api_client(app) as http:
        await login(http, "erika@example.org")
        state = (await http.get("/todo-export")).json()
        response = await http.post("/todo-export/lists", json=CONNECTION)
    await app.state.database.dispose()

    assert state == {"available_sinks": [], "target": None}
    assert response.status_code == 422
    assert response.json()["error_code"] == "sink_not_available"


async def test_list_task_lists(erika: AsyncClient, fake_sink: FakeSink) -> None:
    response = await erika.post("/todo-export/lists", json=CONNECTION)

    assert response.status_code == 200
    assert response.json() == [
        {"id": "list-1", "name": "Tasks"},
        {"id": "list-2", "name": "Work"},
    ]


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [(SinkAuthError(), 422, "auth_failed"), (SinkUnavailableError(), 502, "unavailable")],
)
async def test_connection_errors(
    erika: AsyncClient, fake_sink: FakeSink, error: Exception, status: int, code: str
) -> None:
    fake_sink.error = error  # type: ignore[assignment]

    response = await erika.post("/todo-export/lists", json=CONNECTION)

    assert response.status_code == status
    assert response.json()["error_code"] == code


async def test_invalid_url_with_the_real_sink(
    erika: AsyncClient, sink_holder: dict[str, Any]
) -> None:
    sink_holder["builder"] = service.create_sink
    response = await erika.post(
        "/todo-export/lists", json={**CONNECTION, "url": "http://dav.example.org/"}
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "insecure_url"


async def test_connect_change_and_disconnect(
    erika: AsyncClient, db_session: AsyncSession, queued: list[uuid.UUID]
) -> None:
    body = await connect(erika)

    target = body["target"]
    assert body["available_sinks"] == ["caldav"]
    assert target["url"] == CONNECTION["url"]
    assert target["username"] == "erika"
    assert target["has_password"] is True
    assert "password" not in target and "app-password-1" not in str(body)
    assert (target["list_id"], target["list_name"], target["mode"]) == ("list-1", "Tasks", "auto")
    assert target["active"] is True
    assert target["counts"] == {"synced": 0, "pending": 0, "error": 0, "removed": 0}
    row = await db_session.scalar(select(TodoExportTarget))
    assert row is not None and len(queued) == 1 and queued[0] == row.id
    # Encrypted at rest.
    raw = await db_session.scalar(text("SELECT config FROM todo_export_targets"))
    assert isinstance(raw, str) and "app-password-1" not in raw and "erika" not in raw

    # Another list without repeating the password.
    body = await connect(erika, list_id="list-2", password=None, mode="manual")
    assert (body["target"]["list_name"], body["target"]["mode"]) == ("Work", "manual")
    patched = await erika.patch("/todo-export", json={"mode": "auto"})
    assert patched.json()["target"]["mode"] == "auto"

    assert (await erika.delete("/todo-export")).status_code == 204
    assert (await erika.get("/todo-export")).json()["target"] is None
    assert (await erika.delete("/todo-export")).status_code == 404
    changes = [
        row.details["change"]
        for row in (
            await db_session.execute(
                select(audit_events).where(
                    audit_events.c.action == AuditAction.TODO_EXPORT_CHANGED.value
                )
            )
        ).all()
    ]
    assert changes == ["connected", "updated", "updated", "disconnected"]


async def test_stored_password_is_only_reused_for_the_same_account(
    erika: AsyncClient, fake_sink: FakeSink, sink_holder: dict[str, Any]
) -> None:
    await connect(erika)
    seen: list[dict[str, Any]] = []

    def build(kind: str, config: dict[str, Any], settings: TodosSettings) -> FakeSink:
        seen.append(dict(config))
        return fake_sink

    sink_holder["builder"] = build
    await erika.post("/todo-export/lists", json={**CONNECTION, "password": None})
    await erika.post(
        "/todo-export/lists",
        json={**CONNECTION, "url": "https://other.example.org/", "password": None},
    )

    assert [config["password"] for config in seen] == ["app-password-1", ""]


async def test_unknown_list_is_rejected(erika: AsyncClient) -> None:
    response = await erika.put(
        "/todo-export", json={**CONNECTION, "list_id": "/somewhere/else/", "mode": "auto"}
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "unknown_list"


async def test_another_account_starts_over(
    erika: AsyncClient, db_session: AsyncSession, fake_sink: FakeSink
) -> None:
    await connect(erika)
    uid = await user_id(erika)
    todo = Todo(user_id=uid, title="Exported")
    db_session.add(todo)
    await db_session.flush()
    first = await service.get_target(db_session, uid)
    assert first is not None
    await service.sync_target(
        db_session,
        first.id,
        settings=TodosSettings(export_sinks=["caldav"]),
        sink_builder=builder_for(fake_sink),
    )

    await connect(erika, username="erika2")

    second = await service.get_target(db_session, uid)
    assert second is not None and second.id != first.id
    await db_session.refresh(todo)
    assert todo.external_refs == {}


async def test_todo_changes_queue_a_sync(
    erika: AsyncClient, db_session: AsyncSession, queued: list[uuid.UUID]
) -> None:
    # Without a target nothing is queued.
    created = await erika.post("/todos", json={"title": "Before"})
    assert created.status_code == 201 and queued == []
    await connect(erika)
    queued.clear()

    created = await erika.post("/todos", json={"title": "Water the plants"})
    todo_id = created.json()["id"]
    assert created.json()["export_state"] is None
    await erika.patch(f"/todos/{todo_id}", json={"status": "done"})

    target = await service.get_target(db_session, await user_id(erika))
    assert target is not None
    assert queued == [target.id, target.id]


async def test_deleting_an_exported_todo_deletes_the_copy(
    erika: AsyncClient, db_session: AsyncSession, queued: list[uuid.UUID], fake_sink: FakeSink
) -> None:
    await connect(erika)
    todo_id = (await erika.post("/todos", json={"title": "Exported"})).json()["id"]
    target = await service.get_target(db_session, await user_id(erika))
    assert target is not None
    await service.sync_target(
        db_session,
        target.id,
        settings=TodosSettings(export_sinks=["caldav"]),
        sink_builder=builder_for(fake_sink),
    )
    exported = (await erika.get(f"/todos/{todo_id}")).json()
    assert exported["export_state"]["state"] == "synced"
    queued.clear()

    assert (await erika.delete(f"/todos/{todo_id}")).status_code == 204

    assert queued == [target.id]
    await db_session.refresh(target)
    assert target.pending_deletions == [{"list": "list-1", "id": f"list-1/{todo_id}"}]


async def test_export_a_single_todo(
    erika: AsyncClient, db_session: AsyncSession, queued: list[uuid.UUID]
) -> None:
    todo_id = (await erika.post("/todos", json={"title": "Chosen"})).json()["id"]
    assert (await erika.post(f"/todo-export/todos/{todo_id}")).status_code == 404
    await connect(erika, mode="manual")
    queued.clear()

    response = await erika.post(f"/todo-export/todos/{todo_id}")

    assert response.status_code == 200
    assert response.json()["export_state"] == {
        "sink": "caldav",
        "state": "pending",
        "synced_at": None,
        "error": None,
    }
    assert len(queued) == 1
    counts = (await erika.get("/todo-export")).json()["target"]["counts"]
    assert counts["pending"] == 1


async def test_foreign_and_team_todos_cannot_be_exported(
    erika: AsyncClient, db_session: AsyncSession
) -> None:
    await connect(erika)
    other = await make_local_user(db_session, "bob@example.org")
    foreign = Todo(user_id=other.id, title="Bob's task")
    team = await make_mailbox(db_session, None, "team@example.org")
    from app.mail.models import MailboxAssignment

    db_session.add(MailboxAssignment(mailbox_id=team.id, user_id=await user_id(erika)))
    unassigned = Todo(user_id=None, mailbox_id=team.id, title="Team task")
    db_session.add_all([foreign, unassigned])
    await db_session.flush()

    assert (await erika.post(f"/todo-export/todos/{foreign.id}")).status_code == 404
    response = await erika.post(f"/todo-export/todos/{unassigned.id}")
    assert response.status_code == 422
    assert response.json()["error_code"] == "not_exportable"


async def test_sync_now(erika: AsyncClient, queued: list[uuid.UUID]) -> None:
    assert (await erika.post("/todo-export/sync")).status_code == 404
    await connect(erika)
    queued.clear()

    assert (await erika.post("/todo-export/sync")).status_code == 202
    assert len(queued) == 1


async def test_settings_are_in_the_data_export_without_password(
    erika: AsyncClient, db_session: AsyncSession, tmp_path: Any
) -> None:
    await connect(erika)

    content = await collect(db_session, await user_id(erika), DigestStorage(tmp_path))

    exported = content.documents["todo_export.json"]
    assert isinstance(exported, dict)
    assert exported["url"] == CONNECTION["url"]
    assert exported["list_name"] == "Tasks"
    assert "password" not in exported and "config" not in exported


async def test_end_to_end_with_a_caldav_server(
    caldav_server: CalDAVServer,
    calendar: str,
    settings: Settings,
    db_session: AsyncSession,
    queued: list[uuid.UUID],
) -> None:
    app_settings = export_settings(settings, export_allow_http=True)
    app = create_app(app_settings)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    async def enqueue(target_id: uuid.UUID) -> None:
        queued.append(target_id)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_export_enqueuer] = lambda: enqueue
    user = await make_local_user(db_session, "erika@example.org")
    mailbox = await make_mailbox(db_session, user, "erika@example.org")
    connection = {
        "sink": "caldav",
        "url": caldav_server.url,
        "username": "erika",
        "password": CALDAV_USERS["erika"],
    }
    try:
        async with api_client(app) as http:
            await login(http, "erika@example.org")
            lists = (await http.post("/todo-export/lists", json=connection)).json()
            assert calendar in [item["id"] for item in lists]
            saved = await http.put(
                "/todo-export", json={**connection, "list_id": calendar, "mode": "auto"}
            )
            assert saved.status_code == 200
            created = await http.post(
                "/todos", json={"title": "Prüfe Angebot, Preise; Fristen", "due_date": "2026-10-09"}
            )
            todo_id = created.json()["id"]
            todo = await db_session.get(Todo, uuid.UUID(todo_id))
            assert todo is not None
            todo.mailbox_id = mailbox.id
            await db_session.flush()

            sync = await service.sync_target(
                db_session,
                queued[0],
                settings=app_settings.todos,
                public_url="https://mail.example.org",
            )
            assert sync is not None and (sync.created, sync.error) == (1, None)
            body = caldav_server.get(f"{calendar}{todo_id}.ics").text
            assert "SUMMARY:Prüfe Angebot\\, Preise\\; Fristen" in body
            assert "STATUS:NEEDS-ACTION" in body

            # Completed in another app: ollamail takes it over with the status check.
            completed = body.replace("STATUS:NEEDS-ACTION", "STATUS:COMPLETED").replace(
                "LAST-MODIFIED:", "X-OLD-MODIFIED:"
            )
            assert caldav_server.put(f"{calendar}{todo_id}.ics", completed).status_code < 300
            await service.sync_target(
                db_session, queued[0], settings=app_settings.todos, force_poll=True
            )
            # The API shares this test session; production requests have their own.
            db_session.expire_all()
            fetched = (await http.get(f"/todos/{todo_id}")).json()
            assert fetched["status"] == TodoStatus.DONE.value
            assert fetched["export_state"]["state"] == "synced"

            # Reopened in ollamail: sent back.
            await http.patch(f"/todos/{todo_id}", json={"status": "open"})
            await service.sync_target(db_session, queued[0], settings=app_settings.todos)
            assert "STATUS:NEEDS-ACTION" in caldav_server.get(f"{calendar}{todo_id}.ics").text

            # Deleted in ollamail: deleted there.
            await http.delete(f"/todos/{todo_id}")
            await service.sync_target(db_session, queued[0], settings=app_settings.todos)
            assert caldav_server.get(f"{calendar}{todo_id}.ics").status_code == 404
    finally:
        await app.state.database.dispose()
