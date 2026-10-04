"""Unlinking the own sign-in methods and blocking their relinking (#216)."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditAction
from app.auth.mfa.models import Passkey
from app.auth.models import AuthSession, Identity, IdentityLinkBlock, IdentityLinkNotice
from app.auth.providers.base import VerifiedIdentity
from app.auth.provisioning import (
    ProvisioningError,
    ProvisioningErrorCode,
    ProvisioningPolicy,
    provision_user,
)
from app.auth.reauth import REAUTH_REQUIRED
from app.auth.sessions import SESSION_COOKIE, create_session
from app.core.config import Settings
from app.core.db import get_db
from app.main import create_app
from app.scim.models import ScimUser
from app.users.models import User
from tests.audit.conftest import audit_rows
from tests.auth.conftest import login, make_local_user
from tests.auth.test_link_notices import _linked_user
from tests.auth.test_roles import FakeRedirectProvider
from tests.conftest import api_client
from tests.scim.conftest import scim_config

pytestmark = pytest.mark.db

Clients = tuple[FastAPI, AsyncClient, AsyncClient]

IDENTITY = VerifiedIdentity(
    provider="oidc:test", subject="1", email="erika@example.org", email_verified=True
)


@pytest.fixture
async def app_and_clients(settings: Settings, db_session: AsyncSession) -> AsyncIterator[Clients]:
    """One app with the provider ``oidc:test`` and two browsers: local and through the IdP."""
    app = create_app(settings)
    app.state.auth_providers.register(FakeRedirectProvider())

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    async with api_client(app) as local, api_client(app) as linked:
        yield app, local, linked
    await app.state.database.dispose()


async def _oidc_identity_id(local: AsyncClient) -> str:
    (identity,) = [
        i for i in (await local.get("/auth/identities")).json() if i["provider"] != "local"
    ]
    return str(identity["id"])


async def _count(db: AsyncSession, model: type) -> int:
    return await db.scalar(select(func.count()).select_from(model)) or 0


async def test_lists_the_own_sign_in_methods(
    app_and_clients: Clients, db_session: AsyncSession, settings: Settings
) -> None:
    _, local, linked = app_and_clients
    await _linked_user(db_session, settings, local, linked)

    from_local = {i["provider"]: i for i in (await local.get("/auth/identities")).json()}
    from_linked = {i["provider"]: i for i in (await linked.get("/auth/identities")).json()}

    assert from_local.keys() == {"local", "oidc:test"}
    assert from_local["local"]["current"] is True
    assert from_local["local"]["unlink_refusal"] == "local"
    assert from_local["oidc:test"]["provider_name"] == "Test IdP"
    assert from_local["oidc:test"]["current"] is False
    assert from_local["oidc:test"]["unlink_refusal"] is None
    assert from_local["oidc:test"]["created_at"]
    # The linked provider cannot unlink itself (nor the local sign-in).
    assert from_linked["oidc:test"]["current"] is True
    assert from_linked["oidc:test"]["unlink_refusal"] == "current_session"


async def test_scim_and_pending_invitations_are_no_sign_in_methods(
    app_and_clients: Clients, db_session: AsyncSession, settings: Settings
) -> None:
    _, local, linked = app_and_clients
    user = await _linked_user(db_session, settings, local, linked)
    db_session.add(Identity(user_id=user.id, provider="scim", subject="scim-1"))
    await db_session.execute(
        update(Identity)
        .where(Identity.user_id == user.id, Identity.provider == "local")
        .values(password_hash=None)
    )
    await db_session.commit()

    providers = [i["provider"] for i in (await linked.get("/auth/identities")).json()]

    assert providers == ["oidc:test"]


async def test_passkeys_count_as_local_sign_in(
    app_and_clients: Clients, db_session: AsyncSession, settings: Settings
) -> None:
    _, local, linked = app_and_clients
    user = await _linked_user(db_session, settings, local, linked)
    await db_session.execute(
        update(Identity)
        .where(Identity.user_id == user.id, Identity.provider == "local")
        .values(password_hash=None)
    )
    db_session.add(
        Passkey(
            user_id=user.id,
            credential_id=b"credential",
            public_key=b"key",
            sign_count=0,
            name="Laptop",
        )
    )
    await db_session.commit()

    providers = [i["provider"] for i in (await linked.get("/auth/identities")).json()]

    assert providers == ["local", "oidc:test"]


async def test_unlinking_ends_sessions_drops_notices_and_blocks_relinking(
    app_and_clients: Clients, db_session: AsyncSession, settings: Settings
) -> None:
    _, local, linked = app_and_clients
    user = await _linked_user(db_session, settings, local, linked)
    identity_id = await _oidc_identity_id(local)

    response = await local.delete(f"/auth/identities/{identity_id}")

    assert response.status_code == 204
    assert [i["provider"] for i in (await local.get("/auth/identities")).json()] == ["local"]
    # The sessions of the provider are gone, the own one stays.
    assert (await linked.get("/auth/me")).status_code == 401
    assert (await local.get("/auth/me")).status_code == 200
    providers = set(await db_session.scalars(select(AuthSession.provider)))
    assert providers == {"local"}
    assert await _count(db_session, IdentityLinkNotice) == 0
    (block,) = (await local.get("/auth/link-blocks")).json()
    assert block["provider"] == "oidc:test"
    assert block["provider_name"] == "Test IdP"
    (event,) = await audit_rows(db_session, AuditAction.USER_IDENTITY_UNLINKED)
    assert event.actor_kind == "user"
    assert event.actor_id == user.id
    assert event.target_id == str(user.id)
    assert event.details == {"provider": "oidc:test", "sessions": 1}

    # The next sign-in through the provider does not link the account again.
    with pytest.raises(ProvisioningError) as refused:
        await provision_user(db_session, IDENTITY, ProvisioningPolicy(link_by_email=True))
    assert refused.value.code is ProvisioningErrorCode.EMAIL_CONFLICT
    assert await _count(db_session, IdentityLinkNotice) == 0


async def test_the_block_also_stops_scim_linking(
    db_session: AsyncSession,
) -> None:
    user = await make_local_user(db_session)
    db_session.add(ScimUser(user_id=user.id, user_name="erika", external_id="e-1"))
    db_session.add(IdentityLinkBlock(user_id=user.id, provider="oidc:test"))
    config = await scim_config(db_session)
    config.link_providers = ["oidc:test"]
    await db_session.commit()

    with pytest.raises(ProvisioningError) as refused:
        await provision_user(db_session, IDENTITY, ProvisioningPolicy())

    assert refused.value.code is ProvisioningErrorCode.EMAIL_CONFLICT


async def test_the_block_applies_only_to_its_user_and_provider(db_session: AsyncSession) -> None:
    user = await make_local_user(db_session)
    other = await make_local_user(db_session, "max@example.org")
    db_session.add(IdentityLinkBlock(user_id=user.id, provider="oidc:other"))
    db_session.add(IdentityLinkBlock(user_id=other.id, provider="oidc:test"))
    await db_session.commit()

    result = await provision_user(db_session, IDENTITY, ProvisioningPolicy(link_by_email=True))

    assert result.linked and result.user.id == user.id


async def test_lifting_the_block_allows_linking_again(
    app_and_clients: Clients, db_session: AsyncSession, settings: Settings
) -> None:
    _, local, linked = app_and_clients
    user = await _linked_user(db_session, settings, local, linked)
    await local.delete(f"/auth/identities/{await _oidc_identity_id(local)}")
    (block,) = (await local.get("/auth/link-blocks")).json()

    response = await local.delete(f"/auth/link-blocks/{block['id']}")

    assert response.status_code == 204
    assert (await local.get("/auth/link-blocks")).json() == []
    (event,) = await audit_rows(db_session, AuditAction.USER_IDENTITY_LINK_UNBLOCKED)
    assert (event.actor_kind, event.actor_id) == ("user", user.id)
    assert event.details == {"provider": "oidc:test"}
    result = await provision_user(db_session, IDENTITY, ProvisioningPolicy(link_by_email=True))
    assert result.linked and result.user.id == user.id
    assert (await local.delete(f"/auth/link-blocks/{block['id']}")).status_code == 404


async def test_sessions_of_the_blocked_provider_neither_see_nor_lift_it(
    app_and_clients: Clients, db_session: AsyncSession, settings: Settings
) -> None:
    """E.g. through a second identity at the same provider that stayed linked."""
    _, local, linked = app_and_clients
    user = await _linked_user(db_session, settings, local, linked)
    await local.delete(f"/auth/identities/{await _oidc_identity_id(local)}")
    (block,) = (await local.get("/auth/link-blocks")).json()
    token = await create_session(
        db_session, settings.auth, user, provider="oidc:test", user_agent=None
    )
    await db_session.commit()
    linked.cookies.set(SESSION_COOKIE, token)

    assert (await linked.get("/auth/link-blocks")).json() == []
    assert (await linked.delete(f"/auth/link-blocks/{block['id']}")).status_code == 404
    assert len((await local.get("/auth/link-blocks")).json()) == 1


async def test_unlinking_twice_keeps_one_block(
    app_and_clients: Clients, db_session: AsyncSession, settings: Settings
) -> None:
    _, local, linked = app_and_clients
    user = await _linked_user(db_session, settings, local, linked)
    db_session.add(IdentityLinkBlock(user_id=user.id, provider="oidc:test"))
    await db_session.commit()

    response = await local.delete(f"/auth/identities/{await _oidc_identity_id(local)}")

    assert response.status_code == 204
    assert await _count(db_session, IdentityLinkBlock) == 1


async def test_the_current_sessions_method_cannot_be_unlinked(
    app_and_clients: Clients, db_session: AsyncSession, settings: Settings
) -> None:
    _, local, linked = app_and_clients
    await _linked_user(db_session, settings, local, linked)
    identity_id = await _oidc_identity_id(local)

    response = await linked.delete(f"/auth/identities/{identity_id}")

    assert response.status_code == 409
    assert response.json()["type"] == "urn:ollamail:problem:unlink-current-session"
    assert response.json()["reason"] == "current_session"
    assert await _count(db_session, IdentityLinkBlock) == 0


async def test_local_sign_in_cannot_be_unlinked(
    app_and_clients: Clients, db_session: AsyncSession, settings: Settings
) -> None:
    _, local, linked = app_and_clients
    await _linked_user(db_session, settings, local, linked)
    (own,) = [i for i in (await linked.get("/auth/identities")).json() if i["provider"] == "local"]

    response = await linked.delete(f"/auth/identities/{own['id']}")

    assert response.status_code == 409
    assert response.json()["reason"] == "local"


async def test_the_last_sign_in_method_cannot_be_unlinked(
    app_and_clients: Clients, db_session: AsyncSession, settings: Settings
) -> None:
    """A session of a provider the user has no identity at (any more) is all that is left."""
    _, _, client = app_and_clients
    result = await provision_user(db_session, IDENTITY, ProvisioningPolicy())
    token = await create_session(
        db_session, settings.auth, result.user, provider="ldap:old", user_agent=None
    )
    await db_session.commit()
    client.cookies.set(SESSION_COOKIE, token)
    (identity,) = (await client.get("/auth/identities")).json()
    assert identity["unlink_refusal"] == "last_sign_in"

    response = await client.delete(f"/auth/identities/{identity['id']}")

    assert response.status_code == 409
    assert response.json()["reason"] == "last_sign_in"


async def test_unlinking_needs_a_recent_confirmation(
    app_and_clients: Clients, db_session: AsyncSession, settings: Settings
) -> None:
    _, local, linked = app_and_clients
    user = await _linked_user(db_session, settings, local, linked)
    identity_id = await _oidc_identity_id(local)
    db_session.add(IdentityLinkBlock(user_id=user.id, provider="github:corp"))
    await db_session.execute(
        update(AuthSession).values(authenticated_at=datetime.now(UTC) - timedelta(hours=1))
    )
    await db_session.commit()
    (block,) = (await local.get("/auth/link-blocks")).json()

    unlink = await local.delete(f"/auth/identities/{identity_id}")
    lift = await local.delete(f"/auth/link-blocks/{block['id']}")

    assert unlink.status_code == 403
    assert unlink.json()["type"] == REAUTH_REQUIRED
    assert lift.status_code == 403
    assert lift.json()["type"] == REAUTH_REQUIRED
    assert await _count(db_session, IdentityLinkBlock) == 1


async def test_identities_of_other_users_are_out_of_reach(
    app_and_clients: Clients, db_session: AsyncSession, settings: Settings
) -> None:
    _, local, linked = app_and_clients
    await _linked_user(db_session, settings, local, linked)
    identity_id = await _oidc_identity_id(local)
    await make_local_user(db_session, "max@example.org")
    assert (await login(linked, "max@example.org")).status_code == 200

    assert [i["provider"] for i in (await linked.get("/auth/identities")).json()] == ["local"]
    assert (await linked.delete(f"/auth/identities/{identity_id}")).status_code == 404
    assert await _count(db_session, Identity) == 3


async def test_blocks_are_deleted_with_the_user(db_session: AsyncSession) -> None:
    user = await make_local_user(db_session)
    db_session.add(IdentityLinkBlock(user_id=user.id, provider="oidc:test"))
    await db_session.flush()

    await db_session.delete(await db_session.get(User, user.id))
    await db_session.flush()

    assert await _count(db_session, IdentityLinkBlock) == 0


async def test_identity_endpoints_require_authentication(db_client: AsyncClient) -> None:
    some_id = "00000000-0000-0000-0000-000000000000"
    assert (await db_client.get("/auth/identities")).status_code == 401
    assert (await db_client.delete(f"/auth/identities/{some_id}")).status_code == 401
    assert (await db_client.get("/auth/link-blocks")).status_code == 401
    assert (await db_client.delete(f"/auth/link-blocks/{some_id}")).status_code == 401
