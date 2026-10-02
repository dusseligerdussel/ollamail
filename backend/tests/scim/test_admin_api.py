"""Admin API for SCIM: switch, endpoint URL, tokens (shown once), audit entries."""

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.models import audit_events
from app.users.models import UserRole
from tests.auth.conftest import login, make_local_user
from tests.conftest import api_client
from tests.scim.conftest import idp_client

pytestmark = pytest.mark.db


async def test_admin_enables_scim_and_manages_tokens(
    app: FastAPI, admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    settings = (await admin_client.get("/admin/scim")).json()
    assert settings == {
        "enabled": False,
        "endpoint_url": "https://test/api/scim/v2",
        "link_providers": [],
        "tokens": [],
        "stats": {"users": 0, "active_users": 0, "groups": 0},
    }

    created = await admin_client.post("/admin/scim/tokens", json={"name": "Entra ID"})
    assert created.status_code == 201
    issued = created.json()
    secret = issued["secret"]
    assert secret.startswith("olm_scim_") and len(secret) > 40
    assert secret.startswith(issued["token"]["hint"])

    # Off: the token is valid but SCIM refuses.
    async with idp_client(app, secret) as idp:
        assert (await idp.get("/Users")).status_code == 403

    updated = await admin_client.patch(
        "/admin/scim", json={"enabled": True, "link_providers": ["oidc:entra"]}
    )
    assert updated.status_code == 200
    body = updated.json()
    assert body["enabled"] is True
    assert body["link_providers"] == ["oidc:entra"]
    # The secret is never returned again.
    assert "secret" not in body["tokens"][0]
    assert secret not in updated.text

    async with idp_client(app, secret) as idp:
        assert (await idp.post("/Users", json={"userName": "max@example.org"})).status_code == 201
        stats = (await admin_client.get("/admin/scim")).json()["stats"]
        assert stats == {"users": 1, "active_users": 1, "groups": 0}

        token_id = issued["token"]["id"]
        assert (await admin_client.delete(f"/admin/scim/tokens/{token_id}")).status_code == 204
        assert (await idp.get("/Users")).status_code == 401
    assert (await admin_client.delete(f"/admin/scim/tokens/{token_id}")).status_code == 404

    changes = list(
        await db_session.scalars(
            select(audit_events.c.details)
            .where(audit_events.c.action == "idp.config_changed")
            .order_by(audit_events.c.occurred_at, audit_events.c.id)
        )
    )
    assert [c["change"] for c in changes] == [
        "token_created",
        "enabled",
        "link_providers",
        "token_revoked",
    ]
    assert all(c["kind"] == "scim" for c in changes)


async def test_validation(admin_client: AsyncClient) -> None:
    bad = await admin_client.patch("/admin/scim", json={"link_providers": ["Not A Key!"]})
    assert bad.status_code == 422
    empty = await admin_client.post("/admin/scim/tokens", json={"name": "  "})
    assert empty.status_code == 422
    expiring = await admin_client.post(
        "/admin/scim/tokens", json={"name": "Okta", "expires_in_days": 30}
    )
    assert expiring.json()["token"]["expires_at"] is not None


async def test_requires_admin(app: FastAPI, db_session: AsyncSession) -> None:
    async with api_client(app) as anonymous:
        assert (await anonymous.get("/admin/scim")).status_code == 401
    user = await make_local_user(db_session, "user@example.org", role=UserRole.USER)
    async with api_client(app) as client:
        assert (await login(client, user.email)).status_code == 200
        assert (await client.get("/admin/scim")).status_code == 403
        assert (await client.post("/admin/scim/tokens", json={"name": "x"})).status_code == 403
