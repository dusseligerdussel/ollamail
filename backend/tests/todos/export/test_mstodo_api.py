"""Integration tests: Microsoft To Do connect flow (sign-in mocked with ``respx``), saving
the target, and storing rotated tokens after a sync. PostgreSQL needed."""

import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, TodosSettings
from app.core.db import get_db
from app.main import create_app
from app.todos.export import service
from app.todos.export.models import TodoExportTarget
from app.todos.export.mstodo_router import _FLOW_AAD, FLOW_COOKIE, TOKEN_COOKIE, unseal
from app.todos.export.router import get_export_enqueuer, get_sink_builder
from app.users.models import User
from tests.auth.conftest import _cheap_hashing, login, make_local_user  # noqa: F401
from tests.conftest import api_client
from tests.mail.graph_helpers import GRAPH_HOST, TOKEN_URL, graph_settings, token_response
from tests.todos.export.conftest import FakeSink, make_target

pytestmark = pytest.mark.db

SINKS = TodosSettings(export_sinks=["caldav", "mstodo"])


@dataclass
class RotatingSink(FakeSink):
    """Fake target that records its config and rotates the refresh token once used."""

    kind = "mstodo"
    configs: list[dict[str, Any]] = field(default_factory=list)
    rotate: bool = False

    def updated_config(self) -> Mapping[str, Any] | None:
        if not self.rotate:
            return None
        return {**self.configs[-1], "refresh_token": "refresh-rotated"}


@pytest.fixture
def fake_sink() -> RotatingSink:
    return RotatingSink()


@pytest.fixture
def queued() -> list[uuid.UUID]:
    return []


@pytest.fixture
def app_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"todos": SINKS, "graph": graph_settings()})


@pytest.fixture
async def app(
    app_settings: Settings,
    db_session: AsyncSession,
    queued: list[uuid.UUID],
    fake_sink: RotatingSink,
) -> AsyncIterator[FastAPI]:
    app = create_app(app_settings)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    async def enqueue(target_id: uuid.UUID) -> None:
        queued.append(target_id)

    def build(kind: str, config: Mapping[str, Any], settings: TodosSettings) -> RotatingSink:
        fake_sink.configs.append(dict(config))
        return fake_sink

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


def mock_microsoft(router: respx.MockRouter, *, refresh: str | None = "refresh-new") -> respx.Route:
    token = router.post(TOKEN_URL).mock(return_value=token_response("access-new", refresh))
    router.get(host=GRAPH_HOST, path="/v1.0/me").respond(
        json={"id": "graph-user-1", "userPrincipalName": "erika@example.com"}
    )
    return token


async def start(client: AsyncClient) -> dict[str, list[str]]:
    response = await client.post("/todo-export/mstodo/connect", json={})
    assert response.status_code == 200, response.text
    return parse_qs(urlsplit(response.json()["authorization_url"]).query)


async def sign_in(client: AsyncClient, *, refresh: str | None = "refresh-new") -> httpx.Response:
    params = await start(client)
    with respx.mock(assert_all_called=False) as router:
        mock_microsoft(router, refresh=refresh)
        return await client.get(
            "/todo-export/mstodo/callback",
            params={"code": "auth-code", "state": params["state"][0]},
        )


def redirect(response: httpx.Response) -> tuple[str, dict[str, list[str]]]:
    assert response.status_code == 303
    location = urlsplit(response.headers["location"])
    return location.path, parse_qs(location.query)


async def test_requires_sign_in(app: FastAPI) -> None:
    async with api_client(app) as anonymous:
        assert (await anonymous.post("/todo-export/mstodo/connect", json={})).status_code == 401
        assert (await anonymous.post("/todo-export/mstodo/lists")).status_code == 401


async def test_connect_asks_for_tasks_permission(
    erika: AsyncClient, app_settings: Settings
) -> None:
    params = await start(erika)

    assert params["scope"][0].split() == [
        "offline_access",
        "https://graph.microsoft.com/User.Read",
        "https://graph.microsoft.com/Tasks.ReadWrite",
    ]
    assert params["redirect_uri"] == ["https://test/api/todo-export/mstodo/callback"]
    assert params["code_challenge_method"] == ["S256"]
    flow = unseal(app_settings, erika.cookies[FLOW_COOKIE], _FLOW_AAD)
    assert flow is not None and flow["return_to"] == "/settings/task-export"


async def test_sign_in_pick_a_list_and_save(
    erika: AsyncClient,
    db_session: AsyncSession,
    fake_sink: RotatingSink,
    queued: list[uuid.UUID],
) -> None:
    response = await sign_in(erika)

    path, query = redirect(response)
    assert (path, query) == ("/settings/task-export", {"mstodo": ["connected"]})
    assert FLOW_COOKIE not in erika.cookies
    # Nothing stored before the user picks a list.
    assert await db_session.scalar(select(TodoExportTarget)) is None
    assert TOKEN_COOKIE in erika.cookies
    assert "refresh-new" not in erika.cookies[TOKEN_COOKIE]

    lists = await erika.post("/todo-export/mstodo/lists")
    assert lists.status_code == 200, lists.text
    assert lists.json() == {
        "account": "erika@example.com",
        "lists": [{"id": "list-1", "name": "Tasks"}, {"id": "list-2", "name": "Work"}],
    }
    assert fake_sink.configs[-1]["refresh_token"] == "refresh-new"

    saved = await erika.put("/todo-export/mstodo", json={"list_id": "list-2", "mode": "manual"})
    assert saved.status_code == 200, saved.text
    target = saved.json()["target"]
    assert target["sink"] == "mstodo"
    assert target["username"] == "erika@example.com"
    assert (target["url"], target["has_password"]) == ("", False)
    assert (target["list_name"], target["mode"]) == ("Work", "manual")
    assert "refresh" not in saved.text
    assert TOKEN_COOKIE not in erika.cookies
    row = await db_session.scalar(select(TodoExportTarget))
    assert row is not None and queued == [row.id]
    assert row.config["refresh_token"] == "refresh-new"
    assert row.config["account_id"] == "graph-user-1"
    raw = await db_session.scalar(text("SELECT config FROM todo_export_targets"))
    assert isinstance(raw, str) and "refresh-new" not in raw and "erika" not in raw

    # Changing the list later uses the stored tokens.
    again = await erika.put("/todo-export/mstodo", json={"list_id": "list-1", "mode": "auto"})
    assert again.json()["target"]["list_name"] == "Tasks"
    assert fake_sink.configs[-1]["refresh_token"] == "refresh-new"


async def test_lists_without_sign_in_need_the_connect_flow(erika: AsyncClient) -> None:
    response = await erika.post("/todo-export/mstodo/lists")

    assert response.status_code == 409
    assert response.json()["error_code"] == "mstodo_not_connected"


async def test_unknown_list_is_rejected(erika: AsyncClient) -> None:
    await sign_in(erika)

    response = await erika.put("/todo-export/mstodo", json={"list_id": "nope"})

    assert response.status_code == 422
    assert response.json()["error_code"] == "unknown_list"


async def test_rotated_tokens_are_kept(
    erika: AsyncClient, db_session: AsyncSession, fake_sink: RotatingSink
) -> None:
    await sign_in(erika)
    fake_sink.rotate = True

    await erika.post("/todo-export/mstodo/lists")
    await erika.put("/todo-export/mstodo", json={"list_id": "list-1"})

    # The second call got the token rotated by the first one (kept in the cookie).
    assert fake_sink.configs[-1]["refresh_token"] == "refresh-rotated"
    row = await db_session.scalar(select(TodoExportTarget))
    assert row is not None and row.config["refresh_token"] == "refresh-rotated"


async def test_another_target_is_replaced(erika: AsyncClient, db_session: AsyncSession) -> None:
    user = await db_session.scalar(select(User).where(User.email == "erika@example.org"))
    assert user is not None
    caldav = await make_target(db_session, user)
    await db_session.commit()
    await sign_in(erika)

    saved = await erika.put("/todo-export/mstodo", json={"list_id": "list-1"})

    assert saved.json()["target"]["sink"] == "mstodo"
    rows = list(await db_session.scalars(select(TodoExportTarget)))
    assert [row.sink for row in rows] == ["mstodo"] and rows[0].id != caldav.id


@pytest.mark.parametrize(
    ("params", "reason"),
    [
        ({"code": "c", "state": "forged"}, "state_invalid"),
        ({"error": "access_denied"}, "consent_denied"),
    ],
)
async def test_callback_errors_redirect_with_a_code(
    erika: AsyncClient, params: dict[str, str], reason: str
) -> None:
    flow = await start(erika)
    if "state" not in params:
        params = {**params, "state": flow["state"][0]}

    response = await erika.get("/todo-export/mstodo/callback", params=params)

    assert redirect(response)[1] == {"mstodo": ["error"], "reason": [reason]}
    assert TOKEN_COOKIE not in erika.cookies


async def test_sign_in_without_refresh_token_fails(erika: AsyncClient) -> None:
    response = await sign_in(erika, refresh=None)

    assert redirect(response)[1]["reason"] == ["offline_access_missing"]


async def test_not_offered_unless_enabled(
    settings: Settings, db_session: AsyncSession, app_settings: Settings
) -> None:
    for disabled, code in (
        (settings.model_copy(update={"graph": graph_settings()}), "sink_not_available"),
        (settings.model_copy(update={"todos": SINKS}), "not_configured"),
    ):
        app = create_app(disabled)

        async def override_get_db() -> AsyncIterator[AsyncSession]:
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        if await db_session.scalar(select(User).where(User.email == "lena@example.org")) is None:
            await make_local_user(db_session, "lena@example.org")
        async with api_client(app) as http:
            await login(http, "lena@example.org")
            response = await http.post("/todo-export/mstodo/connect", json={})
        await app.state.database.dispose()

        assert response.status_code == 422
        assert response.json()["error_code"] == code


async def test_generic_connect_rejects_oauth_targets(erika: AsyncClient) -> None:
    response = await erika.post(
        "/todo-export/lists", json={"sink": "mstodo", "url": "https://graph.microsoft.com"}
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "oauth_required"


async def test_sync_stores_rotated_tokens(
    db_session: AsyncSession, fake_sink: RotatingSink
) -> None:
    user = await make_local_user(db_session, "erika@example.org")
    target = await make_target(db_session, user, config={"refresh_token": "refresh-1"})
    await db_session.commit()
    fake_sink.rotate = True

    def build(kind: str, config: Mapping[str, Any], settings: TodosSettings) -> RotatingSink:
        fake_sink.configs.append(dict(config))
        return fake_sink

    await service.sync_target(db_session, target.id, settings=SINKS, sink_builder=build)

    await db_session.refresh(target)
    assert target.config["refresh_token"] == "refresh-rotated"


async def test_sync_keeps_a_newer_connection(
    db_session: AsyncSession, fake_sink: RotatingSink
) -> None:
    user = await make_local_user(db_session, "erika@example.org")
    target = await make_target(db_session, user, config={"refresh_token": "refresh-1"})
    await db_session.commit()
    fake_sink.rotate = True

    def build(kind: str, config: Mapping[str, Any], settings: TodosSettings) -> RotatingSink:
        fake_sink.configs.append(dict(config))
        # The user signs in again while the sync runs.
        target.config = {"refresh_token": "refresh-reconnected"}
        return fake_sink

    await service.sync_target(db_session, target.id, settings=SINKS, sink_builder=build)

    await db_session.refresh(target)
    assert target.config["refresh_token"] == "refresh-reconnected"
