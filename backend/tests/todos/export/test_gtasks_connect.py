"""Integration tests: connecting Google Tasks with OAuth (API + PostgreSQL), picking another
list of a connected target. Google's token endpoint is mocked with respx, the Tasks API by
the in-memory sink. Names, IDs and tokens are invented."""

import base64
import hashlib
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditAction
from app.audit.models import audit_events
from app.core.config import GmailSettings, Settings, TodosSettings
from app.core.db import get_db
from app.mail.providers.gmail_auth import SCOPE_MODIFY, SCOPE_TASKS, TOKEN_URL
from app.mail.providers.gmail_connect import STATE_COOKIE as GMAIL_STATE_COOKIE
from app.mail.providers.gmail_connect import encode_state_cookie
from app.main import create_app
from app.todos.export.base import TaskList
from app.todos.export.gtasks_connect import STATE_COOKIE, redirect_uri
from app.todos.export.models import ExportMode, TodoExportTarget
from app.todos.export.router import get_export_enqueuer, get_sink_builder
from app.todos.models import Todo
from tests.auth.conftest import _cheap_hashing, login, make_local_user  # noqa: F401
from tests.conftest import api_client
from tests.todos.export.conftest import FakeSink

pytestmark = pytest.mark.db

CALLBACK = "https://test/api/todo-export/gtasks/oauth/callback"


def gtasks_settings(settings: Settings, sinks: list[str] | None = None) -> Settings:
    return settings.model_copy(
        update={
            "todos": TodosSettings(export_sinks=sinks if sinks is not None else ["gtasks"]),
            "gmail": GmailSettings(
                client_id="client-id.apps.googleusercontent.com",
                client_secret="test-client-secret",
                redirect_uri="https://test/api/mail/gmail/oauth/callback",
            ),
        }
    )


@pytest.fixture
def app_settings(settings: Settings) -> Settings:
    return gtasks_settings(settings)


@pytest.fixture
def queued() -> list[uuid.UUID]:
    return []


@pytest.fixture
def google_lists() -> FakeSink:
    sink = FakeSink()
    sink.lists = [TaskList("gl-default", "Meine Aufgaben"), TaskList("gl-work", "Arbeit")]
    return sink


@pytest.fixture
def built() -> list[dict[str, Any]]:
    """Configs the sink was built with."""
    return []


@pytest.fixture
async def app(
    app_settings: Settings,
    db_session: AsyncSession,
    queued: list[uuid.UUID],
    google_lists: FakeSink,
    built: list[dict[str, Any]],
) -> AsyncIterator[FastAPI]:
    app = create_app(app_settings)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    async def enqueue(target_id: uuid.UUID) -> None:
        queued.append(target_id)

    def build(kind: str, config: Any, settings: TodosSettings) -> FakeSink:
        built.append({"kind": kind, **config})
        return google_lists

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_export_enqueuer] = lambda: enqueue
    app.dependency_overrides[get_sink_builder] = lambda: build
    yield app
    await app.state.database.dispose()


@pytest.fixture
async def erika(app: FastAPI, db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    await make_local_user(db_session, "erika@example.org")
    async with api_client(app) as http:
        assert (await login(http, "erika@example.org")).status_code == 200
        yield http


@pytest.fixture
def google() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


def token_response(scope: str, refresh_token: str | None = "rt-tasks-1") -> httpx.Response:
    body = {"access_token": "at", "expires_in": 3599, "scope": scope, "token_type": "Bearer"}
    if refresh_token:
        body["refresh_token"] = refresh_token
    return httpx.Response(200, json=body)


async def start(client: AsyncClient, mode: str = "auto") -> dict[str, str]:
    response = await client.post("/todo-export/gtasks/oauth/start", json={"mode": mode})
    assert response.status_code == 200, response.json()
    url = urlparse(response.json()["authorization_url"])
    return {key: values[0] for key, values in parse_qs(url.query).items()}


async def callback(client: AsyncClient, state: str, **params: str) -> httpx.Response:
    return await client.get(
        "/todo-export/gtasks/oauth/callback", params={"code": "auth-code", "state": state, **params}
    )


async def targets(session: AsyncSession) -> list[TodoExportTarget]:
    rows = await session.scalars(select(TodoExportTarget).execution_options(populate_existing=True))
    return list(rows)


def test_redirect_uri_is_derived_from_the_gmail_one(settings: Settings) -> None:
    configured = gtasks_settings(settings)
    assert redirect_uri(configured) == CALLBACK

    explicit = configured.model_copy(
        update={
            "todos": TodosSettings(export_gtasks_redirect_uri="https://mail.example.org/cb"),
        }
    )
    assert redirect_uri(explicit) == "https://mail.example.org/cb"

    other = configured.model_copy(
        update={"gmail": GmailSettings(redirect_uri="https://mail.example.org/oauth")}
    )
    assert redirect_uri(other) == "https://mail.example.org/api/todo-export/gtasks/oauth/callback"


async def test_requires_sign_in(app: FastAPI) -> None:
    async with api_client(app) as anonymous:
        assert (await anonymous.post("/todo-export/gtasks/oauth/start", json={})).status_code == 401
        assert (await anonymous.get("/todo-export/gtasks/oauth/callback")).status_code == 401


@pytest.mark.parametrize("sinks", [[], ["caldav"]])
async def test_start_needs_the_admin_opt_in(
    settings: Settings, db_session: AsyncSession, sinks: list[str]
) -> None:
    app = create_app(gtasks_settings(settings, sinks))

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    await make_local_user(db_session, "erika@example.org")
    async with api_client(app) as http:
        await login(http, "erika@example.org")
        response = await http.post("/todo-export/gtasks/oauth/start", json={})
    await app.state.database.dispose()

    assert response.status_code == 422
    assert response.json()["error_code"] == "sink_not_available"


async def test_start_without_oauth_client(erika: AsyncClient, app_settings: Settings) -> None:
    app_settings.gmail.client_id = None

    response = await erika.post("/todo-export/gtasks/oauth/start", json={})

    assert response.status_code == 503
    assert response.json()["error_code"] == "oauth_not_configured"


async def test_start_asks_for_the_tasks_scope(erika: AsyncClient) -> None:
    params = await start(erika)

    assert params["scope"] == SCOPE_TASKS
    assert params["include_granted_scopes"] == "true"
    assert params["access_type"] == "offline"
    assert params["redirect_uri"] == CALLBACK
    assert params["code_challenge_method"] == "S256"
    cookie = next(c for c in erika.cookies.jar if c.name == STATE_COOKIE)
    assert cookie.has_nonstandard_attr("HttpOnly")


async def test_callback_connects_google_tasks(
    erika: AsyncClient,
    db_session: AsyncSession,
    google: respx.MockRouter,
    queued: list[uuid.UUID],
    built: list[dict[str, Any]],
) -> None:
    params = await start(erika, mode="manual")
    token = google.post(TOKEN_URL).mock(return_value=token_response(SCOPE_TASKS))

    response = await callback(erika, params["state"])

    assert response.status_code == 303
    assert response.headers["location"] == "/settings/task-export?gtasks=connected"
    [target] = await targets(db_session)
    assert (target.sink, target.list_id, target.list_name, target.mode) == (
        "gtasks",
        "gl-default",
        "Meine Aufgaben",
        ExportMode.MANUAL,
    )
    assert target.config == {"refresh_token": "rt-tasks-1"}
    assert built == [{"kind": "gtasks", "refresh_token": "rt-tasks-1"}]
    raw = await db_session.scalar(
        text("SELECT config FROM todo_export_targets WHERE id = :id"), {"id": target.id}
    )
    assert "rt-tasks-1" not in raw
    assert queued == [target.id]
    # PKCE and the redirect URI of this flow.
    sent = parse_qs(token.calls[0].request.content.decode())
    verifier = sent["code_verifier"][0]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    assert challenge.rstrip(b"=").decode() == params["code_challenge"]
    assert sent["redirect_uri"] == [CALLBACK]
    assert STATE_COOKIE not in {c.name for c in erika.cookies.jar}
    [event] = (
        await db_session.execute(
            select(audit_events).where(
                audit_events.c.action == AuditAction.TODO_EXPORT_CHANGED.value
            )
        )
    ).all()
    assert event.details == {"sink": "gtasks", "change": "connected", "mode": "manual"}

    settings = (await erika.get("/todo-export")).json()
    assert settings["target"]["sink"] == "gtasks"
    assert settings["target"]["has_password"] is True
    assert "rt-tasks-1" not in str(settings)


async def test_callback_without_tasks_scope(
    erika: AsyncClient, db_session: AsyncSession, google: respx.MockRouter
) -> None:
    params = await start(erika)
    google.post(TOKEN_URL).mock(return_value=token_response(SCOPE_MODIFY))

    response = await callback(erika, params["state"])

    assert response.headers["location"] == "/settings/task-export?gtasks_error=insufficient_scope"
    assert await targets(db_session) == []


async def test_callback_cancelled_at_google(erika: AsyncClient, db_session: AsyncSession) -> None:
    params = await start(erika)

    response = await erika.get(
        "/todo-export/gtasks/oauth/callback",
        params={"state": params["state"], "error": "access_denied"},
    )

    assert response.headers["location"] == "/settings/task-export?gtasks_error=access_denied"
    assert await targets(db_session) == []


async def test_callback_rejects_a_wrong_state(erika: AsyncClient, google: respx.MockRouter) -> None:
    await start(erika)
    token = google.post(TOKEN_URL).mock(return_value=token_response(SCOPE_TASKS))

    response = await callback(erika, "forged")

    assert response.headers["location"] == "/settings/task-export?gtasks_error=invalid_state"
    assert not token.called


async def test_callback_rejects_a_gmail_flow_cookie(
    erika: AsyncClient, app_settings: Settings, google: respx.MockRouter
) -> None:
    """Both flows sign with the same key; a Gmail cookie must not finish this flow."""
    me = (await erika.get("/auth/me")).json()["id"]
    cookie = encode_state_cookie(
        app_settings, {"state": "s", "verifier": "v", "user": me, "exp": 9e12}
    )
    erika.cookies.set(STATE_COOKIE, cookie)
    erika.cookies.set(GMAIL_STATE_COOKIE, cookie)
    token = google.post(TOKEN_URL).mock(return_value=token_response(SCOPE_TASKS))

    response = await callback(erika, "s")

    assert response.headers["location"] == "/settings/task-export?gtasks_error=invalid_state"
    assert not token.called


async def test_callback_with_a_revoked_grant(erika: AsyncClient, google: respx.MockRouter) -> None:
    params = await start(erika)
    google.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))

    response = await callback(erika, params["state"])

    assert response.headers["location"] == "/settings/task-export?gtasks_error=token_revoked"


async def test_reconnect_same_account_keeps_target(
    erika: AsyncClient, db_session: AsyncSession, google: respx.MockRouter
) -> None:
    google.post(TOKEN_URL).mock(
        side_effect=[
            token_response(SCOPE_TASKS, "rt-tasks-1"),
            token_response(SCOPE_TASKS, "rt-tasks-2"),
        ]
    )
    await callback(erika, (await start(erika))["state"])
    [first] = await targets(db_session)
    assert (await erika.patch("/todo-export", json={"list_id": "gl-work"})).status_code == 200

    response = await callback(erika, (await start(erika, mode="manual"))["state"])

    assert response.headers["location"] == "/settings/task-export?gtasks=connected"
    [target] = await targets(db_session)
    # Same account (its list is still there): list and mode stay, only the token changes.
    assert (target.id, target.list_id, target.mode) == (first.id, "gl-work", ExportMode.AUTO)
    assert target.config == {"refresh_token": "rt-tasks-2"}


async def test_connecting_another_account_starts_over(
    erika: AsyncClient,
    db_session: AsyncSession,
    google: respx.MockRouter,
    google_lists: FakeSink,
) -> None:
    google.post(TOKEN_URL).mock(return_value=token_response(SCOPE_TASKS))
    await callback(erika, (await start(erika))["state"])
    [first] = await targets(db_session)
    user_id = first.user_id
    todo = Todo(
        user_id=user_id,
        title="Testaufgabe",
        external_refs={"gtasks": {"target": str(first.id), "list": "gl-default", "id": "t1"}},
    )
    db_session.add(todo)
    await db_session.commit()
    google_lists.lists = [TaskList("gl-other", "Andere")]

    await callback(erika, (await start(erika))["state"])

    [target] = await targets(db_session)
    assert target.id != first.id
    assert target.list_id == "gl-other"
    await db_session.refresh(todo)
    assert todo.external_refs == {}


async def test_callback_without_any_list(
    erika: AsyncClient, google: respx.MockRouter, google_lists: FakeSink
) -> None:
    google_lists.lists = []
    google.post(TOKEN_URL).mock(return_value=token_response(SCOPE_TASKS))

    response = await callback(erika, (await start(erika))["state"])

    assert response.headers["location"] == "/settings/task-export?gtasks_error=no_lists"


async def test_credentials_form_is_refused_for_google_tasks(erika: AsyncClient) -> None:
    connection = {"sink": "gtasks", "url": "https://tasks.googleapis.com"}

    listed = await erika.post("/todo-export/lists", json=connection)
    saved = await erika.put("/todo-export", json={**connection, "list_id": "gl-default"})

    for response in (listed, saved):
        assert response.status_code == 422
        assert response.json()["error_code"] == "oauth_required"


async def test_pick_another_list(
    erika: AsyncClient,
    db_session: AsyncSession,
    google: respx.MockRouter,
    queued: list[uuid.UUID],
) -> None:
    google.post(TOKEN_URL).mock(return_value=token_response(SCOPE_TASKS))
    await callback(erika, (await start(erika))["state"])

    lists = await erika.get("/todo-export/lists")
    unknown = await erika.patch("/todo-export", json={"list_id": "gl-nope"})
    changed = await erika.patch("/todo-export", json={"list_id": "gl-work"})

    assert lists.json() == [
        {"id": "gl-default", "name": "Meine Aufgaben"},
        {"id": "gl-work", "name": "Arbeit"},
    ]
    assert unknown.status_code == 422
    assert unknown.json()["error_code"] == "unknown_list"
    assert changed.status_code == 200
    target = changed.json()["target"]
    assert (target["list_id"], target["list_name"], target["mode"]) == (
        "gl-work",
        "Arbeit",
        "auto",
    )
    assert len(queued) == 2


async def test_lists_need_a_connected_target(erika: AsyncClient) -> None:
    assert (await erika.get("/todo-export/lists")).status_code == 404
