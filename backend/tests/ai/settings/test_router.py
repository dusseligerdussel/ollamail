"""Admin API for AI settings and the cloud status for users."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditAction
from app.auth.models import AuthSession
from app.core.config import LLMSettings, Settings
from app.users.models import User, UserRole
from tests.audit.conftest import audit_rows
from tests.auth.conftest import login, make_local_user

pytestmark = pytest.mark.db

CLOUD = {
    "name": "cloud",
    "display_name": "Example Cloud",
    "kind": "openai_compatible",
    "base_url": "https://api.cloud.test/v1/",
    "api_key": "sk-very-secret",
    "is_cloud": True,
}


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"llm": LLMSettings(base_url="http://ollama.test:11434")})


async def _sign_in(client: AsyncClient, db: AsyncSession, role: UserRole) -> User:
    user = await make_local_user(db, f"{role}@example.org", role=role)
    assert (await login(client, user.email)).status_code == 200
    return user


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/admin/ai/settings"),
        ("PATCH", "/admin/ai/settings"),
        ("GET", "/admin/ai/providers"),
        ("POST", "/admin/ai/providers"),
        ("POST", "/admin/ai/providers/default/test"),
        ("DELETE", "/admin/ai/providers/cloud"),
    ],
)
async def test_only_admins_have_access(
    db_client: AsyncClient, db_session: AsyncSession, method: str, path: str
) -> None:
    assert (await db_client.request(method, path, json={})).status_code == 401
    await _sign_in(db_client, db_session, UserRole.USER)
    assert (await db_client.request(method, path, json={})).status_code == 403


async def test_environment_provider_is_listed_read_only(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _sign_in(db_client, db_session, UserRole.ADMIN)

    (default,) = (await db_client.get("/admin/ai/providers")).json()
    patch = await db_client.patch("/admin/ai/providers/default", json={"display_name": "X"})
    delete = await db_client.delete("/admin/ai/providers/default")

    assert default["name"] == "default"
    assert default["source"] == "environment"
    assert default["used_by"] == [
        "triage",
        "todos",
        "digest",
        "rag_chat",
        "reply_draft",
        "embeddings",
    ]
    assert patch.status_code == delete.status_code == 409


async def test_create_provider_never_returns_the_api_key(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin = await _sign_in(db_client, db_session, UserRole.ADMIN)

    response = await db_client.post("/admin/ai/providers", json=CLOUD)
    listing = await db_client.get("/admin/ai/providers")
    raw = await db_session.scalar(text("SELECT api_key FROM ai_providers"))

    assert response.status_code == 201
    created = response.json()
    assert created["api_key_set"] is True
    assert created["base_url"] == "https://api.cloud.test/v1"
    assert created["source"] == "database"
    assert "sk-very-secret" not in response.text
    assert "sk-very-secret" not in listing.text
    assert raw is not None and "sk-very-secret" not in raw
    (event,) = await audit_rows(db_session, AuditAction.AI_SETTINGS_CHANGED)
    assert event.actor_id == admin.id
    assert event.details == {"change": "provider_created", "provider": "cloud", "is_cloud": True}
    assert (await db_client.post("/admin/ai/providers", json=CLOUD)).status_code == 409


async def test_update_keeps_or_removes_the_api_key(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _sign_in(db_client, db_session, UserRole.ADMIN)
    await db_client.post("/admin/ai/providers", json=CLOUD)

    kept = await db_client.patch("/admin/ai/providers/cloud", json={"display_name": "Renamed"})
    removed = await db_client.patch("/admin/ai/providers/cloud", json={"api_key": None})

    assert kept.json()["display_name"] == "Renamed"
    assert kept.json()["api_key_set"] is True
    assert removed.json()["api_key_set"] is False
    assert (await db_client.patch("/admin/ai/providers/missing", json={})).status_code == 404


@pytest.mark.parametrize(
    "base_url", ["ftp://host", "http://user:pw@host", "not a url", "http://host?x=1"]
)
async def test_invalid_base_url_is_rejected(
    db_client: AsyncClient, db_session: AsyncSession, base_url: str
) -> None:
    await _sign_in(db_client, db_session, UserRole.ADMIN)

    response = await db_client.post("/admin/ai/providers", json={**CLOUD, "base_url": base_url})

    assert response.status_code == 422


@respx.mock
async def test_connection_test_lists_models_or_reports_the_error(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _sign_in(db_client, db_session, UserRole.ADMIN)
    respx.get("http://ollama.test:11434/api/tags").respond(
        json={"models": [{"name": "qwen2.5:3b"}, {"name": "bge-m3:latest"}]}
    )
    models = respx.get("https://api.cloud.test/v1/models").respond(401)
    await db_client.post("/admin/ai/providers", json=CLOUD)

    ok = (await db_client.post("/admin/ai/providers/default/test")).json()
    denied = (await db_client.post("/admin/ai/providers/cloud/test")).json()

    assert ok["ok"] is True
    assert ok["models"] == ["bge-m3:latest", "qwen2.5:3b"]
    assert denied == {
        "ok": False,
        "models": [],
        "error": "unauthorized",
        "status_code": 401,
        "duration_ms": denied["duration_ms"],
    }
    assert models.calls.last.request.headers["authorization"] == "Bearer sk-very-secret"


async def _age_sessions(db: AsyncSession) -> None:
    """Sign-ins older than the confirmation limit (``OLLAMAIL_AUTH_REAUTH_MINUTES``)."""
    await db.execute(
        update(AuthSession).values(authenticated_at=datetime.now(UTC) - timedelta(minutes=60))
    )
    await db.commit()


@respx.mock
async def test_unsaved_settings_can_be_tested_with_the_stored_key(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _sign_in(db_client, db_session, UserRole.ADMIN)
    await db_client.post("/admin/ai/providers", json=CLOUD)
    route = respx.get("https://api.cloud.test/v1/models").respond(json={"data": [{"id": "m1"}]})
    respx.get("http://down.test:11434/api/tags").mock(side_effect=httpx.ConnectError("down"))
    await _age_sessions(db_session)

    stored_key = await db_client.post(
        "/admin/ai/providers/test",
        json={
            "name": "cloud",
            "kind": "openai_compatible",
            "base_url": "https://api.cloud.test/v1",
        },
    )
    unreachable = await db_client.post(
        "/admin/ai/providers/test", json={"kind": "ollama", "base_url": "http://down.test:11434"}
    )

    # Same destination as stored: no confirmation needed.
    assert stored_key.json()["models"] == ["m1"]
    assert route.calls.last.request.headers["authorization"] == "Bearer sk-very-secret"
    assert unreachable.json()["error"] == "unreachable"


@pytest.mark.parametrize(
    "target",
    [
        {"kind": "openai_compatible", "base_url": "https://attacker.test/v1"},
        {"kind": "ollama", "base_url": "https://api.cloud.test/v1"},
    ],
)
@respx.mock
async def test_stored_key_goes_elsewhere_only_after_a_recent_confirmation(
    db_client: AsyncClient, db_session: AsyncSession, target: dict[str, str]
) -> None:
    """#219: a stolen admin cookie must not send the stored key to another server."""
    await _sign_in(db_client, db_session, UserRole.ADMIN)
    await db_client.post("/admin/ai/providers", json=CLOUD)
    route = respx.route(host=httpx.URL(target["base_url"]).host).respond(
        json={"data": [], "models": []}
    )
    await _age_sessions(db_session)

    stale = await db_client.post("/admin/ai/providers/test", json={"name": "cloud", **target})

    assert stale.status_code == 403
    assert stale.json()["type"] == "urn:ollamail:problem:reauth-required"
    assert not route.called

    # With a new key, or after confirming, the test runs.
    own_key = await db_client.post(
        "/admin/ai/providers/test", json={"name": "cloud", "api_key": "sk-other", **target}
    )
    assert own_key.json()["ok"] is True
    assert "sk-very-secret" not in route.calls.last.request.headers.get("authorization", "")
    await db_session.execute(update(AuthSession).values(authenticated_at=datetime.now(UTC)))
    await db_session.commit()
    confirmed = await db_client.post("/admin/ai/providers/test", json={"name": "cloud", **target})
    assert confirmed.json()["ok"] is True


async def test_assign_model_per_task_and_reset(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _sign_in(db_client, db_session, UserRole.ADMIN)
    await db_client.post("/admin/ai/providers", json=CLOUD)

    response = await db_client.patch(
        "/admin/ai/settings",
        json={
            "tasks": {"digest": {"provider": "cloud", "model": "gpt-test"}},
            "profile": "gpu-consumer",
            "concurrency": 2,
        },
    )
    body = response.json()
    digest = next(t for t in body["tasks"] if t["task"] == "digest")
    triage = next(t for t in body["tasks"] if t["task"] == "triage")

    assert response.status_code == 200
    assert digest["effective_provider"] == "cloud"
    assert digest["effective_model"] == "gpt-test"
    assert digest["default_provider"] == "default"
    assert digest["default_model"] == "qwen2.5:14b"
    # Cloud LLMs are still off: the task cannot run.
    assert digest["blocked"] is True
    assert triage["provider"] is None
    assert triage["effective_model"] == "qwen2.5:14b"
    assert body["profile"] == "gpu-consumer"
    assert body["concurrency"] == 2
    # The provider is in use and cannot be deleted.
    assert (await db_client.delete("/admin/ai/providers/cloud")).status_code == 409

    reset = await db_client.patch(
        "/admin/ai/settings",
        json={"tasks": {"digest": {"provider": None, "model": None}}, "profile": None},
    )
    digest = next(t for t in reset.json()["tasks"] if t["task"] == "digest")

    assert digest["effective_provider"] == "default"
    assert reset.json()["profile"] == "cpu"
    assert (await db_client.delete("/admin/ai/providers/cloud")).status_code == 204


@pytest.mark.parametrize(
    "body",
    [
        {"concurrency": 99},
        {"concurrency": 0},
        {"tasks": {"digest": {"provider": "missing", "model": "m"}}},
        {"tasks": {"digest": {"provider": "default"}}},
        {"tasks": {"unknown": {"model": "m"}}},
        {"profile": "huge"},
    ],
)
async def test_invalid_settings_are_rejected(
    db_client: AsyncClient, db_session: AsyncSession, body: dict[str, object]
) -> None:
    await _sign_in(db_client, db_session, UserRole.ADMIN)

    assert (await db_client.patch("/admin/ai/settings", json=body)).status_code == 422


async def test_cloud_switch_is_audited_and_shown_to_users(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _sign_in(db_client, db_session, UserRole.ADMIN)
    await db_client.post("/admin/ai/providers", json=CLOUD)
    await db_client.patch(
        "/admin/ai/settings",
        json={"tasks": {"triage": {"provider": "cloud", "model": "gpt-test"}}},
    )
    assert (await db_client.get("/ai/status")).json() == {"cloud": []}

    enabled = await db_client.patch("/admin/ai/settings", json={"cloud_enabled": True})
    await db_client.post("/auth/logout")
    await _sign_in(db_client, db_session, UserRole.USER)
    status = (await db_client.get("/ai/status")).json()

    assert enabled.json()["cloud_enabled"] is True
    assert status == {
        "cloud": [{"provider": "cloud", "display_name": "Example Cloud", "tasks": ["triage"]}]
    }
    events = await audit_rows(db_session, AuditAction.AI_SETTINGS_CHANGED)
    assert events[-1].details == {"change": "settings_updated", "cloud_enabled": True}


async def test_status_requires_sign_in(db_client: AsyncClient) -> None:
    assert (await db_client.get("/ai/status")).status_code == 401
