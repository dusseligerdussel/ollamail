"""Admin API for OIDC providers: CRUD, validation, encrypted client secret."""

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditAction
from app.users.models import UserRole
from tests.audit.conftest import audit_rows
from tests.auth.conftest import login, make_local_user
from tests.auth.oidc.conftest import PUBLIC_URL, OIDCTestApp
from tests.auth.oidc.mock_idp import CLIENT_SECRET, ISSUER

pytestmark = pytest.mark.db

BASE = "/admin/auth/oidc/providers"
TENANT = "11111111-1111-4111-8111-111111111111"


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "corp",
        "display_name": "Corp SSO",
        "preset": "keycloak",
        "issuer": ISSUER,
        "client_id": "ollamail",
        "client_secret": CLIENT_SECRET,
    }
    body.update(overrides)
    return body


async def _admin(oidc: OIDCTestApp, db: AsyncSession) -> AsyncClient:
    await make_local_user(db, "admin@example.org", role=UserRole.ADMIN)
    assert (await login(oidc.client, "admin@example.org")).status_code == 200
    return oidc.client


async def test_requires_admin(oidc: OIDCTestApp, db_session: AsyncSession) -> None:
    assert (await oidc.client.get(BASE)).status_code == 401
    await make_local_user(db_session)
    await login(oidc.client, "erika@example.org")

    assert (await oidc.client.get(BASE)).status_code == 403
    assert (await oidc.client.post(BASE, json=_body())).status_code == 403


async def test_create_read_update_delete(oidc: OIDCTestApp, db_session: AsyncSession) -> None:
    client = await _admin(oidc, db_session)

    created = await client.post(BASE, json=_body(allowed_domains=["Example.org"]))

    assert created.status_code == 201
    data = created.json()
    assert "client_secret" not in data
    assert data["has_client_secret"] is True
    assert data["provider"] == "oidc:corp"
    assert data["source"] == "db"
    assert data["allowed_domains"] == ["example.org"]
    assert data["redirect_uri"] == f"{PUBLIC_URL}/api/auth/oidc/corp/callback"
    assert (await client.get(f"{BASE}/corp")).json()["display_name"] == "Corp SSO"

    updated = await client.patch(
        f"{BASE}/corp", json={"display_name": "Corporate", "client_secret": None}
    )
    assert updated.status_code == 200
    assert updated.json()["display_name"] == "Corporate"
    assert updated.json()["has_client_secret"] is False
    assert updated.json()["client_id"] == "ollamail"

    assert (await client.delete(f"{BASE}/corp")).status_code == 204
    assert (await client.get(f"{BASE}/corp")).status_code == 404
    assert (await client.get(BASE)).json() == []


async def test_client_secret_is_encrypted_at_rest(
    oidc: OIDCTestApp, db_session: AsyncSession
) -> None:
    client = await _admin(oidc, db_session)
    await client.post(BASE, json=_body())

    stored = await db_session.scalar(text("SELECT client_secret FROM auth_oidc_providers"))

    assert stored is not None
    assert CLIENT_SECRET not in stored


async def test_duplicate_name_conflicts(oidc: OIDCTestApp, db_session: AsyncSession) -> None:
    client = await _admin(oidc, db_session)
    await client.post(BASE, json=_body())

    response = await client.post(BASE, json=_body())

    assert response.status_code == 409


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"name": "Bad Name"}, id="name"),
        pytest.param({"issuer": "http://idp.example.org"}, id="http-issuer"),
        pytest.param({"issuer": "https://user:pw@idp.example.org"}, id="credentials"),
        pytest.param({"scopes": ["email"]}, id="no-openid-scope"),
        pytest.param(
            {"preset": "entra", "issuer": "https://login.microsoftonline.com/organizations/v2.0"},
            id="entra-multi-tenant-without-tenants",
        ),
        pytest.param(
            {"preset": "entra", "issuer": "https://login.microsoftonline.com/contoso.com/v2.0"},
            id="entra-tenant-name",
        ),
        pytest.param({"preset": "entra", "issuer": ISSUER}, id="entra-wrong-host"),
        pytest.param({"allowed_tenants": [TENANT]}, id="tenants-without-entra"),
        pytest.param({"preset": "google", "issuer": ISSUER}, id="google-wrong-issuer"),
        pytest.param({"hosted_domains": ["example.org"]}, id="hd-without-google"),
    ],
)
async def test_invalid_configuration_is_rejected(
    oidc: OIDCTestApp, db_session: AsyncSession, overrides: dict[str, Any]
) -> None:
    client = await _admin(oidc, db_session)

    response = await client.post(BASE, json=_body(**overrides))

    assert response.status_code == 422


async def test_valid_presets_are_accepted(oidc: OIDCTestApp, db_session: AsyncSession) -> None:
    client = await _admin(oidc, db_session)

    entra = await client.post(
        BASE,
        json=_body(
            name="entra",
            preset="entra",
            issuer="https://login.microsoftonline.com/organizations/v2.0",
            allowed_tenants=[TENANT.upper()],
        ),
    )
    google = await client.post(
        BASE,
        json=_body(
            name="google",
            preset="google",
            issuer="https://accounts.google.com",
            hosted_domains=["example.org"],
            groups_claim=None,
        ),
    )

    assert entra.status_code == 201, entra.json()
    assert entra.json()["allowed_tenants"] == [TENANT]
    assert google.status_code == 201, google.json()
    assert google.json()["groups_claim"] is None


async def test_invalid_update_is_rejected(oidc: OIDCTestApp, db_session: AsyncSession) -> None:
    client = await _admin(oidc, db_session)
    await client.post(BASE, json=_body())

    response = await client.patch(f"{BASE}/corp", json={"preset": "google"})

    assert response.status_code == 422
    assert (await client.get(f"{BASE}/corp")).json()["preset"] == "keycloak"


async def test_presets_are_listed(oidc: OIDCTestApp, db_session: AsyncSession) -> None:
    client = await _admin(oidc, db_session)

    presets = {p["preset"]: p for p in (await client.get("/admin/auth/oidc/presets")).json()}

    assert set(presets) == {"generic", "entra", "google", "keycloak", "authentik"}
    assert presets["entra"]["fields"] == ["allowed_tenants"]
    assert presets["google"]["groups_claim"] is None


async def test_configuration_changes_are_audited(
    oidc: OIDCTestApp, db_session: AsyncSession
) -> None:
    client = await _admin(oidc, db_session)
    await client.post(BASE, json=_body())
    await client.patch(f"{BASE}/corp", json={"display_name": "Corporate"})
    await client.delete(f"{BASE}/corp")

    rows = await audit_rows(db_session, AuditAction.IDP_CONFIG_CHANGED)

    assert [r.details for r in rows] == [
        {"kind": "oidc", "change": "created"},
        {"kind": "oidc", "change": "updated"},
        {"kind": "oidc", "change": "deleted"},
    ]
    assert len({r.target_id for r in rows}) == 1
