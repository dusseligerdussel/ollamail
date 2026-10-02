"""Authentication of SCIM requests: bearer tokens (hash only, revocable, expiring), the
switch, rate limits, CSRF exemption and the SCIM error format."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import ScimSettings
from app.scim import tokens
from app.scim.models import ScimToken
from tests.scim.conftest import idp_client, issue_token, ok, scim_config

pytestmark = pytest.mark.db

ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"


async def test_requests_need_a_valid_token(app: FastAPI, scim_token: str) -> None:
    for token in (None, "olm_scim_wrong", scim_token + "x", "Basic abc"):
        async with idp_client(app, token) as client:
            response = await client.get("/Users")
        error = ok(response, 401)
        assert error["schemas"] == [ERROR_SCHEMA]
        assert response.headers["www-authenticate"].startswith("Bearer")
    async with idp_client(app, scim_token) as client:
        assert (await client.get("/Users")).status_code == 200


async def test_only_the_hash_is_stored(db_session: AsyncSession, scim_token: str) -> None:
    stored = await db_session.scalar(select(ScimToken))
    assert stored is not None
    assert stored.token_hash == tokens.hash_token(scim_token)
    assert scim_token not in stored.hint and scim_token.startswith(stored.hint)


async def test_disabled_scim_refuses_requests(idp: AsyncClient, db_session: AsyncSession) -> None:
    config = await scim_config(db_session)
    config.enabled = False
    await db_session.commit()
    error = ok(await idp.get("/Users"), 403)
    assert error["detail"] == "SCIM provisioning is disabled."


async def test_revoked_and_expired_tokens_stop_working(
    app: FastAPI, idp: AsyncClient, db_session: AsyncSession
) -> None:
    token = await db_session.scalar(select(ScimToken))
    assert token is not None
    assert (await idp.get("/Users")).status_code == 200
    assert token.last_used_at is not None

    token.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    assert (await idp.get("/Users")).status_code == 401

    await db_session.delete(token)
    await db_session.commit()
    assert (await idp.get("/Users")).status_code == 401


async def test_rate_limit_per_token(app: FastAPI, idp: AsyncClient) -> None:
    app.state.settings.scim = ScimSettings(rate_limit_per_minute=3)
    for _ in range(3):
        assert (await idp.get("/ServiceProviderConfig")).status_code == 200
    response = await idp.get("/ServiceProviderConfig")
    assert ok(response, 429)["status"] == "429"
    assert int(response.headers["retry-after"]) >= 1


async def test_rate_limit_for_wrong_tokens(app: FastAPI, scim_token: str) -> None:
    app.state.settings.scim = ScimSettings(failed_auth_per_ip=2)
    async with idp_client(app, "olm_scim_guess") as client:
        assert (await client.get("/Users")).status_code == 401
        assert (await client.get("/Users")).status_code == 401
        assert (await client.get("/Users")).status_code == 429


async def test_no_csrf_token_needed_but_cookies_do_not_authenticate(
    app: FastAPI, admin_client: AsyncClient, idp: AsyncClient
) -> None:
    # The IdP client sends neither cookies nor a CSRF token.
    assert (await idp.post("/Groups", json={"displayName": "Sales"})).status_code == 201
    # An admin session is no SCIM credential.
    response = await admin_client.get("/scim/v2/Users")
    assert response.status_code == 401


async def test_errors_use_the_scim_format(idp: AsyncClient) -> None:
    error = ok(await idp.get("/Users/not-a-uuid"), 404)
    assert error == {"schemas": [ERROR_SCHEMA], "status": "404", "detail": "User not found."}
    bad_json = await idp.post("/Users", content=b"{")
    assert ok(bad_json, 400)["scimType"] == "invalidSyntax"
    missing = await idp.post("/Users", json={"displayName": "No user name"})
    assert ok(missing, 400)["scimType"] == "invalidValue"
    unsupported = await idp.get("/Users", params={"filter": 'userName co "a"'})
    assert ok(unsupported, 400)["scimType"] == "invalidFilter"
    disjunction = await idp.get("/Users", params={"filter": 'userName eq "a" or userName eq "b"'})
    assert ok(disjunction, 400)["scimType"] == "invalidFilter"


async def test_discovery_documents(idp: AsyncClient) -> None:
    config = ok(await idp.get("/ServiceProviderConfig"))
    assert config["patch"] == {"supported": True}
    assert config["bulk"]["supported"] is False
    assert config["filter"]["supported"] is True
    assert config["authenticationSchemes"][0]["type"] == "oauthbearertoken"
    assert config["meta"]["location"] == "https://test/api/scim/v2/ServiceProviderConfig"

    types = ok(await idp.get("/ResourceTypes"))
    assert {t["id"] for t in types["Resources"]} == {"User", "Group"}
    assert ok(await idp.get("/ResourceTypes/User"))["endpoint"] == "/Users"

    schemas = ok(await idp.get("/Schemas"))
    ids = [s["id"] for s in schemas["Resources"]]
    assert ids == [
        "urn:ietf:params:scim:schemas:core:2.0:User",
        "urn:ietf:params:scim:schemas:core:2.0:Group",
    ]
    user_schema = ok(await idp.get(f"/Schemas/{ids[0]}"))
    names = {a["name"] for a in user_schema["attributes"]}
    assert {"userName", "externalId", "emails", "active", "groups"} <= names
    assert (await idp.get("/Schemas/urn:unknown")).status_code == 404


async def test_second_token_works_independently(
    app: FastAPI, db_session: AsyncSession, scim_token: str
) -> None:
    other = await issue_token(db_session)
    async with idp_client(app, other) as client:
        assert (await client.get("/Users")).status_code == 200
