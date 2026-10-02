"""Admin API for GitHub providers: CRUD, validation, encrypted secret, audit log."""

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditAction
from app.users.models import UserRole
from tests.audit.conftest import audit_rows
from tests.auth.conftest import login, make_local_user
from tests.auth.github.conftest import CLIENT_ID, CLIENT_SECRET, PUBLIC_URL, GitHubTestApp

pytestmark = pytest.mark.db

BASE = "/admin/auth/github/providers"


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "github",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }
    body.update(overrides)
    return body


async def _admin(gh: GitHubTestApp, db: AsyncSession) -> AsyncClient:
    await make_local_user(db, "admin@example.org", role=UserRole.ADMIN)
    assert (await login(gh.client, "admin@example.org")).status_code == 200
    return gh.client


async def test_requires_admin(gh: GitHubTestApp, db_session: AsyncSession) -> None:
    assert (await gh.client.get(BASE)).status_code == 401
    await make_local_user(db_session)
    await login(gh.client, "erika@example.org")

    assert (await gh.client.get(BASE)).status_code == 403
    assert (await gh.client.post(BASE, json=_body())).status_code == 403


async def test_create_read_update_delete(gh: GitHubTestApp, db_session: AsyncSession) -> None:
    client = await _admin(gh, db_session)

    created = await client.post(
        BASE,
        json=_body(
            allowed_organizations=["Acme", "acme"],
            allowed_teams=["Acme/Mail-Admins"],
            allowed_domains=["Example.org"],
        ),
    )

    assert created.status_code == 201
    data = created.json()
    assert "client_secret" not in data
    assert data["has_client_secret"] is True
    assert data["provider"] == "github:github"
    assert data["display_name"] == "GitHub"
    assert data["base_url"] is None
    assert data["allowed_organizations"] == ["acme"]
    assert data["allowed_teams"] == ["acme/mail-admins"]
    assert data["allowed_domains"] == ["example.org"]
    assert data["redirect_uri"] == f"{PUBLIC_URL}/api/auth/github/github/callback"
    assert (await client.get(f"{BASE}/github")).json()["client_id"] == CLIENT_ID

    updated = await client.patch(
        f"{BASE}/github",
        json={"base_url": "https://github.example.org/", "allowed_teams": [], "enabled": False},
    )
    assert updated.status_code == 200
    assert updated.json()["base_url"] == "https://github.example.org"
    assert updated.json()["allowed_teams"] == []
    assert updated.json()["enabled"] is False
    assert updated.json()["allowed_organizations"] == ["acme"]

    cleared = await client.patch(f"{BASE}/github", json={"base_url": None})
    assert cleared.json()["base_url"] is None

    assert (await client.delete(f"{BASE}/github")).status_code == 204
    assert (await client.get(f"{BASE}/github")).status_code == 404
    assert (await client.get(BASE)).json() == []


async def test_configuration_changes_are_audited(
    gh: GitHubTestApp, db_session: AsyncSession
) -> None:
    client = await _admin(gh, db_session)
    await client.post(BASE, json=_body())
    await client.patch(f"{BASE}/github", json={"display_name": "GitHub (Firma)"})
    await client.delete(f"{BASE}/github")

    rows = await audit_rows(db_session, AuditAction.IDP_CONFIG_CHANGED)

    assert [r.details for r in rows] == [
        {"kind": "github", "change": change} for change in ("created", "updated", "deleted")
    ]
    assert all(r.target_type == "idp" for r in rows)


async def test_client_secret_is_encrypted_at_rest(
    gh: GitHubTestApp, db_session: AsyncSession
) -> None:
    client = await _admin(gh, db_session)
    await client.post(BASE, json=_body())

    stored = await db_session.scalar(text("SELECT client_secret FROM auth_github_providers"))

    assert stored and CLIENT_SECRET not in stored


async def test_client_secret_is_required_and_kept_on_null(
    gh: GitHubTestApp, db_session: AsyncSession
) -> None:
    client = await _admin(gh, db_session)
    body = _body()
    del body["client_secret"]
    assert (await client.post(BASE, json=body)).status_code == 422

    await client.post(BASE, json=_body())
    updated = await client.patch(f"{BASE}/github", json={"client_secret": None})

    assert updated.json()["has_client_secret"] is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": "GitHub!"},
        {"base_url": "http://github.example.org"},
        {"base_url": "https://user@github.example.org"},
        {"base_url": "https://github.example.org?x=1"},
        {"allowed_organizations": ["-acme"]},
        {"allowed_teams": ["acme"]},
        {"allowed_teams": ["acme/"]},
    ],
)
async def test_invalid_settings_are_rejected(
    gh: GitHubTestApp, db_session: AsyncSession, overrides: dict[str, Any]
) -> None:
    client = await _admin(gh, db_session)

    response = await client.post(BASE, json=_body(**overrides))

    assert response.status_code == 422
    assert (await client.get(BASE)).json() == []


async def test_github_com_base_url_is_normalized(
    gh: GitHubTestApp, db_session: AsyncSession
) -> None:
    client = await _admin(gh, db_session)

    created = await client.post(BASE, json=_body(base_url="https://github.com/"))

    assert created.json()["base_url"] is None


async def test_duplicate_name(gh: GitHubTestApp, db_session: AsyncSession) -> None:
    client = await _admin(gh, db_session)
    await client.post(BASE, json=_body())

    response = await client.post(BASE, json=_body())

    assert response.status_code == 409


async def test_disabling_the_only_admin_access_is_refused(
    gh: GitHubTestApp, db_session: AsyncSession
) -> None:
    """The admin signs in with GitHub only (#33): disabling or deleting it would lock out."""
    client = await _admin(gh, db_session)
    assert (await client.post(BASE, json=_body())).status_code == 201
    admin_id = await db_session.scalar(text("SELECT id FROM users"))
    await db_session.execute(
        text(
            "UPDATE auth_identities SET provider = 'github:github', subject = '1', "
            "password_hash = NULL WHERE user_id = :id"
        ),
        {"id": admin_id},
    )
    await db_session.commit()

    disable = await client.patch(f"{BASE}/github", json={"enabled": False})
    delete = await client.delete(f"{BASE}/github")

    assert disable.status_code == 409
    assert disable.json()["type"] == "urn:ollamail:problem:admin-lockout"
    assert delete.status_code == 409
    assert (await client.get(f"{BASE}/github")).json()["enabled"] is True
