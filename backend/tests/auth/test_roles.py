from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.providers import AuthProviderKind, VerifiedIdentity
from app.core.config import Settings
from app.core.db import get_db
from app.main import create_app
from app.users.models import User, UserRole
from tests.auth.conftest import PASSWORD, login, make_local_user
from tests.conftest import api_client

pytestmark = pytest.mark.db

NEW_USER = {
    "email": "Max@Example.org",
    "display_name": "Max",
    "password": PASSWORD,
}


async def _as(client: AsyncClient, db_session: AsyncSession, role: UserRole) -> User:
    user = await make_local_user(db_session, f"{role}@example.org", role=role)
    assert (await login(client, user.email)).status_code == 200
    return user


async def test_user_admin_requires_admin_role(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    assert (await db_client.get("/users")).status_code == 401

    await _as(db_client, db_session, UserRole.USER)

    assert (await db_client.get("/users")).status_code == 403
    assert (await db_client.post("/users", json=NEW_USER)).status_code == 403


async def test_admin_lists_and_creates_users(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _as(db_client, db_session, UserRole.ADMIN)

    created = await db_client.post("/users", json=NEW_USER)

    assert created.status_code == 201
    assert created.json()["email"] == "max@example.org"
    assert created.json()["role"] == "user"
    emails = [user["email"] for user in (await db_client.get("/users")).json()]
    assert emails == ["admin@example.org", "max@example.org"]
    duplicate = await db_client.post("/users", json={**NEW_USER, "email": "MAX@example.org"})
    assert duplicate.status_code == 409


async def test_created_user_can_sign_in(
    db_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    await _as(db_client, db_session, UserRole.ADMIN)
    await db_client.post("/users", json=NEW_USER)
    await db_client.post("/auth/logout")

    response = await login(db_client, "max@example.org")

    assert response.status_code == 200
    assert response.json()["role"] == "user"


async def test_role_is_checked_on_every_request(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin = await _as(db_client, db_session, UserRole.ADMIN)
    assert (await db_client.get("/users")).status_code == 200

    admin.role = UserRole.USER
    await db_session.commit()

    assert (await db_client.get("/users")).status_code == 403


async def test_admin_creates_user_with_short_password(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _as(db_client, db_session, UserRole.ADMIN)

    response = await db_client.post("/users", json={**NEW_USER, "password": "short"})

    assert response.status_code == 422


async def test_update_own_profile(db_client: AsyncClient, db_session: AsyncSession) -> None:
    await _as(db_client, db_session, UserRole.USER)

    response = await db_client.patch(
        "/auth/me", json={"language": "de", "timezone": "Europe/Vienna", "role": "admin"}
    )

    assert response.status_code == 200
    body = response.json()
    assert (body["language"], body["timezone"], body["role"]) == ("de", "Europe/Vienna", "user")
    invalid = await db_client.patch("/auth/me", json={"timezone": "Mars/Olympus"})
    assert invalid.status_code == 422


async def test_registration_is_off_by_default(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await make_local_user(db_session)

    response = await db_client.post("/auth/register", json=NEW_USER)

    assert response.status_code == 403


async def test_registration_when_enabled(
    db_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    settings.auth.local_registration = True
    await make_local_user(db_session)

    response = await db_client.post("/auth/register", json=NEW_USER)

    assert response.status_code == 201
    assert response.json()["role"] == "user"
    assert (await db_client.get("/auth/me")).json()["email"] == "max@example.org"


async def test_registration_needs_an_initialized_instance(
    db_client: AsyncClient, settings: Settings
) -> None:
    settings.auth.local_registration = True

    response = await db_client.post("/auth/register", json=NEW_USER)

    assert response.status_code == 409
    assert response.json()["type"] == "urn:ollamail:problem:not-initialized"


class FakeRedirectProvider:
    name = "oidc:test"
    display_name = "Test IdP"
    kind = AuthProviderKind.REDIRECT
    login_path = "/auth/test/login"

    async def authorization_url(
        self, *, state: str, nonce: str, redirect_uri: str, code_verifier: str
    ) -> str:
        return "https://idp.example.org/authorize"

    async def complete(
        self, *, params: object, nonce: str, redirect_uri: str, code_verifier: str
    ) -> VerifiedIdentity:
        return VerifiedIdentity(provider=self.name, subject="1")


@pytest.fixture
async def app_client(
    settings: Settings, db_session: AsyncSession
) -> AsyncIterator[tuple[FastAPI, AsyncClient]]:
    app = create_app(settings)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    async with api_client(app) as http:
        yield app, http
    await app.state.database.dispose()


async def test_providers_lists_registered_external_providers(
    app_client: tuple[FastAPI, AsyncClient],
) -> None:
    app, client = app_client
    assert (await client.get("/auth/providers")).json() == {
        "local_login": True,
        "local_registration": False,
        "providers": [],
    }

    app.state.auth_providers.register(FakeRedirectProvider())

    providers = (await client.get("/auth/providers")).json()["providers"]
    assert providers == [
        {
            "name": "oidc:test",
            "display_name": "Test IdP",
            "kind": "redirect",
            "login_path": "/auth/test/login",
        }
    ]


async def test_external_identity_maps_to_its_user(db_session: AsyncSession) -> None:
    from app.auth.models import Identity
    from app.auth.service import user_for_identity

    user = await make_local_user(db_session)
    db_session.add(Identity(user_id=user.id, provider="oidc:test", subject="abc"))
    await db_session.flush()

    found = await user_for_identity(db_session, VerifiedIdentity("oidc:test", "abc"))
    missing = await user_for_identity(db_session, VerifiedIdentity("oidc:test", "other"))

    assert found is not None and found.id == user.id
    assert missing is None
    assert (await db_session.scalars(select(Identity))).all()


async def test_events_stream_requires_a_session(db_client: AsyncClient) -> None:
    response = await db_client.get("/events")

    assert response.status_code == 401
