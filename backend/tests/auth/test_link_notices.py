"""Notices about sign-ins linked by e-mail address (#208) and provider names of sessions."""

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import IdentityLinkNotice
from app.auth.providers.base import VerifiedIdentity
from app.auth.provisioning import ProvisioningPolicy, provision_user
from app.auth.sessions import SESSION_COOKIE, create_session
from app.core.config import Settings
from app.core.db import get_db
from app.main import create_app
from app.users.models import User
from tests.auth.conftest import login, make_local_user
from tests.auth.test_roles import FakeRedirectProvider
from tests.conftest import api_client

pytestmark = pytest.mark.db


@pytest.fixture
async def app_and_clients(
    settings: Settings, db_session: AsyncSession
) -> AsyncIterator[tuple[FastAPI, AsyncClient, AsyncClient]]:
    """One app with the provider ``oidc:test`` and two browsers: local and through the IdP."""
    app = create_app(settings)
    app.state.auth_providers.register(FakeRedirectProvider())

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    async with api_client(app) as local, api_client(app) as linked:
        yield app, local, linked
    await app.state.database.dispose()


async def _linked_user(
    db: AsyncSession, settings: Settings, local: AsyncClient, linked: AsyncClient
) -> User:
    """A local user whose account was linked to ``oidc:test``; signed in on both clients."""
    user = await make_local_user(db)
    assert (await login(local, "erika@example.org")).status_code == 200
    identity = VerifiedIdentity(
        provider="oidc:test", subject="1", email="erika@example.org", email_verified=True
    )
    await provision_user(db, identity, ProvisioningPolicy(link_by_email=True))
    token = await create_session(db, settings.auth, user, provider="oidc:test", user_agent=None)
    await db.commit()
    linked.cookies.set(SESSION_COOKIE, token)
    return user


async def test_other_sign_ins_see_the_notice(
    app_and_clients: tuple[FastAPI, AsyncClient, AsyncClient],
    db_session: AsyncSession,
    settings: Settings,
) -> None:
    _, local, linked = app_and_clients
    await _linked_user(db_session, settings, local, linked)

    response = await local.get("/auth/link-notices")

    assert response.status_code == 200
    (notice,) = response.json()
    assert notice["provider"] == "oidc:test"
    assert notice["provider_name"] == "Test IdP"
    assert notice["created_at"]


async def test_the_linked_provider_can_neither_see_nor_dismiss_it(
    app_and_clients: tuple[FastAPI, AsyncClient, AsyncClient],
    db_session: AsyncSession,
    settings: Settings,
) -> None:
    """Whoever signs in through the new link must not be able to hide it."""
    _, local, linked = app_and_clients
    await _linked_user(db_session, settings, local, linked)
    (notice,) = (await local.get("/auth/link-notices")).json()

    assert (await linked.get("/auth/link-notices")).json() == []
    response = await linked.delete(f"/auth/link-notices/{notice['id']}")

    assert response.status_code == 404
    assert len((await local.get("/auth/link-notices")).json()) == 1


async def test_dismissing_removes_the_notice(
    app_and_clients: tuple[FastAPI, AsyncClient, AsyncClient],
    db_session: AsyncSession,
    settings: Settings,
) -> None:
    _, local, linked = app_and_clients
    await _linked_user(db_session, settings, local, linked)
    (notice,) = (await local.get("/auth/link-notices")).json()

    response = await local.delete(f"/auth/link-notices/{notice['id']}")

    assert response.status_code == 204
    assert (await local.get("/auth/link-notices")).json() == []
    assert (await local.delete(f"/auth/link-notices/{notice['id']}")).status_code == 404


async def test_notices_of_other_users_are_invisible(
    app_and_clients: tuple[FastAPI, AsyncClient, AsyncClient],
    db_session: AsyncSession,
    settings: Settings,
) -> None:
    _, local, linked = app_and_clients
    await _linked_user(db_session, settings, local, linked)
    (notice,) = (await local.get("/auth/link-notices")).json()
    await make_local_user(db_session, "max@example.org")
    assert (await login(linked, "max@example.org")).status_code == 200

    assert (await linked.get("/auth/link-notices")).json() == []
    assert (await linked.delete(f"/auth/link-notices/{notice['id']}")).status_code == 404


async def test_notices_are_deleted_with_the_user(
    app_and_clients: tuple[FastAPI, AsyncClient, AsyncClient],
    db_session: AsyncSession,
    settings: Settings,
) -> None:
    _, local, linked = app_and_clients
    user = await _linked_user(db_session, settings, local, linked)

    await db_session.delete(user)
    await db_session.flush()

    count = await db_session.scalar(select(func.count()).select_from(IdentityLinkNotice))
    assert count == 0


async def test_sessions_name_their_sign_in_method(
    app_and_clients: tuple[FastAPI, AsyncClient, AsyncClient],
    db_session: AsyncSession,
    settings: Settings,
) -> None:
    _, local, linked = app_and_clients
    await _linked_user(db_session, settings, local, linked)

    sessions = (await local.get("/auth/sessions")).json()

    by_provider = {session["provider"]: session["provider_name"] for session in sessions}
    assert by_provider == {"local": None, "oidc:test": "Test IdP"}


async def test_link_notice_endpoints_require_authentication(db_client: AsyncClient) -> None:
    assert (await db_client.get("/auth/link-notices")).status_code == 401
    notice_id = "00000000-0000-0000-0000-000000000000"
    assert (await db_client.delete(f"/auth/link-notices/{notice_id}")).status_code == 401
