"""Notices about sign-ins linked by e-mail address (#208) and provider names of sessions."""

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.events import AuditAction
from app.auth.models import Identity, IdentityLinkNotice
from app.auth.providers.base import VerifiedIdentity
from app.auth.provisioning import ProvisioningPolicy, provision_user
from app.auth.sessions import SESSION_COOKIE, create_session
from app.core.config import Settings
from app.core.db import get_db
from app.main import create_app
from app.users.models import User
from tests.audit.conftest import audit_rows
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


async def _link(
    db: AsyncSession, settings: Settings, user: User, provider: str, client: AsyncClient
) -> None:
    """Link ``provider`` to the account by e-mail address and sign ``client`` in through it."""
    identity = VerifiedIdentity(
        provider=provider, subject="1", email="erika@example.org", email_verified=True
    )
    await provision_user(db, identity, ProvisioningPolicy(link_by_email=True))
    token = await create_session(db, settings.auth, user, provider=provider, user_agent=None)
    await db.commit()
    client.cookies.set(SESSION_COOKIE, token)


async def _linked_user(
    db: AsyncSession, settings: Settings, local: AsyncClient, linked: AsyncClient
) -> User:
    """A local user whose account was linked to ``oidc:test``; signed in on both clients."""
    user = await make_local_user(db)
    assert (await login(local, "erika@example.org")).status_code == 200
    await _link(db, settings, user, "oidc:test", linked)
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


async def test_a_second_linked_provider_cannot_hide_the_notices(
    app_and_clients: tuple[FastAPI, AsyncClient, AsyncClient],
    db_session: AsyncSession,
    settings: Settings,
) -> None:
    """#220: an admin links P1 and P2 by e-mail address and signs in through both. Neither
    session may dismiss the other's notice, or the user would see nothing."""
    app, local, linked = app_and_clients
    user = await _linked_user(db_session, settings, local, linked)
    async with api_client(app) as second:
        await _link(db_session, settings, user, "oidc:other", second)
        notices = {n["provider"]: n["id"] for n in (await local.get("/auth/link-notices")).json()}
        assert set(notices) == {"oidc:test", "oidc:other"}

        assert (await linked.get("/auth/link-notices")).json() == []
        assert (await second.get("/auth/link-notices")).json() == []
        response = await linked.delete(f"/auth/link-notices/{notices['oidc:other']}")
        assert response.status_code == 404
        response = await second.delete(f"/auth/link-notices/{notices['oidc:test']}")
        assert response.status_code == 404

    assert len((await local.get("/auth/link-notices")).json()) == 2


async def test_a_confirmed_provider_sees_later_notices(
    app_and_clients: tuple[FastAPI, AsyncClient, AsyncClient],
    db_session: AsyncSession,
    settings: Settings,
) -> None:
    """Once the user confirmed P1 ("that was me"), it is one of their own sign-in methods."""
    app, local, linked = app_and_clients
    user = await _linked_user(db_session, settings, local, linked)
    (first,) = (await local.get("/auth/link-notices")).json()
    assert (await local.delete(f"/auth/link-notices/{first['id']}")).status_code == 204
    async with api_client(app) as second:
        await _link(db_session, settings, user, "oidc:other", second)

    (notice,) = (await linked.get("/auth/link-notices")).json()
    assert notice["provider"] == "oidc:other"
    assert (await linked.delete(f"/auth/link-notices/{notice['id']}")).status_code == 204


async def test_a_method_linked_after_the_notice_cannot_dismiss_it(
    app_and_clients: tuple[FastAPI, AsyncClient, AsyncClient],
    db_session: AsyncSession,
    settings: Settings,
) -> None:
    """A sign-in method the account got after the link is no witness for it, even without an
    open notice of its own."""
    app, local, linked = app_and_clients
    user = await _linked_user(db_session, settings, local, linked)
    (notice,) = (await local.get("/auth/link-notices")).json()
    row = await db_session.get(IdentityLinkNotice, notice["id"])
    assert row is not None
    db_session.add(
        Identity(
            user_id=user.id,
            provider="oidc:other",
            subject="2",
            created_at=row.created_at + timedelta(minutes=1),
        )
    )
    token = await create_session(
        db_session, settings.auth, user, provider="oidc:other", user_agent=None
    )
    await db_session.commit()
    async with api_client(app) as later:
        later.cookies.set(SESSION_COOKIE, token)

        assert (await later.get("/auth/link-notices")).json() == []
        assert (await later.delete(f"/auth/link-notices/{notice['id']}")).status_code == 404


async def test_dismissing_is_audited(
    app_and_clients: tuple[FastAPI, AsyncClient, AsyncClient],
    db_session: AsyncSession,
    settings: Settings,
) -> None:
    _, local, linked = app_and_clients
    user = await _linked_user(db_session, settings, local, linked)
    (notice,) = (await local.get("/auth/link-notices")).json()

    assert (await local.delete(f"/auth/link-notices/{notice['id']}")).status_code == 204

    (event,) = await audit_rows(db_session, AuditAction.USER_IDENTITY_LINK_CONFIRMED)
    assert (event.actor_kind, event.actor_id) == ("user", user.id)
    assert event.details == {"provider": "oidc:test"}


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
