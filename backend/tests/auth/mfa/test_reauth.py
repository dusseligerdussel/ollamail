"""Confirmation before sensitive actions (#144, app/auth/reauth.py)."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditAction
from app.auth.mfa.models import PendingLogin, TotpFactor
from app.auth.models import AuthSession, Identity
from app.core.config import Settings
from app.core.db import get_db
from app.main import create_app
from app.users.models import User
from tests.audit.conftest import audit_rows
from tests.auth.conftest import PASSWORD, login, make_local_user
from tests.auth.mfa.conftest import add_passkey, code_at, enable_totp
from tests.auth.test_roles import FakeRedirectProvider
from tests.conftest import api_client

pytestmark = pytest.mark.db

EMAIL = "erika@example.org"
REAUTH_REQUIRED = "urn:ollamail:problem:reauth-required"


async def _signed_in(client: AsyncClient, db: AsyncSession) -> None:
    await make_local_user(db, EMAIL)
    assert (await login(client, EMAIL)).status_code == 200


async def _age_sessions(db: AsyncSession, minutes: int = 11) -> None:
    """Pretend the sign-in was ``minutes`` ago (default limit: 10)."""
    await db.execute(
        update(AuthSession).values(authenticated_at=datetime.now(UTC) - timedelta(minutes=minutes))
    )
    await db.commit()


async def test_sessions_start_authenticated(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    before = datetime.now(UTC)
    await _signed_in(db_client, db_session)

    (session,) = (await db_session.scalars(select(AuthSession))).all()
    options = (await db_client.get("/auth/reauth")).json()

    assert session.authenticated_at >= before - timedelta(seconds=1)
    assert options["reauth_minutes"] == 10
    assert options["valid_until"] is not None
    assert options["methods"] == ["password", "signin"]


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("DELETE", "/auth/mfa/totp", None),
        ("POST", "/auth/mfa/recovery-codes", None),
        ("POST", "/privacy/exports", None),
        ("DELETE", "/privacy/account", {"confirm_email": EMAIL}),
    ],
)
async def test_sensitive_actions_need_a_recent_confirmation(
    db_client: AsyncClient, db_session: AsyncSession, method: str, path: str, body: object
) -> None:
    await _signed_in(db_client, db_session)
    await enable_totp(db_client)
    await _age_sessions(db_session)

    response = await db_client.request(method, path, json=body)

    assert response.status_code == 403, response.text
    assert response.json()["type"] == REAUTH_REQUIRED
    assert response.json()["reauth_minutes"] == 10
    # Nothing happened.
    assert await db_session.scalar(select(TotpFactor.id)) is not None
    assert await db_session.scalar(select(User.id).where(User.email == EMAIL)) is not None


async def test_passkey_removal_needs_a_recent_confirmation(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _signed_in(db_client, db_session)
    _, passkey = await add_passkey(db_client)
    await _age_sessions(db_session)

    response = await db_client.delete(f"/auth/mfa/passkeys/{passkey['passkey']['id']}")

    assert response.status_code == 403
    assert response.json()["type"] == REAUTH_REQUIRED


async def test_password_confirms_for_the_configured_minutes(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _signed_in(db_client, db_session)
    await enable_totp(db_client)
    await _age_sessions(db_session)
    assert (await db_client.get("/auth/reauth")).json()["valid_until"] is None

    confirmed = await db_client.post(
        "/auth/reauth", json={"method": "password", "password": PASSWORD}
    )
    removed = await db_client.delete("/auth/mfa/totp")

    assert confirmed.status_code == 200, confirmed.text
    body = confirmed.json()
    valid_until = datetime.fromisoformat(body["valid_until"])
    authenticated_at = datetime.fromisoformat(body["authenticated_at"])
    assert valid_until - authenticated_at == timedelta(minutes=10)
    assert removed.status_code == 204
    (event,) = await audit_rows(db_session, AuditAction.REAUTHENTICATED)
    assert event.details == {"method": "password"}


async def test_wrong_password_is_rejected_audited_and_throttled(
    db_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    settings.auth.login_max_attempts = 2
    await _signed_in(db_client, db_session)
    await _age_sessions(db_session)

    wrong = [
        await db_client.post("/auth/reauth", json={"method": "password", "password": "nope"})
        for _ in range(3)
    ]
    correct = await db_client.post(
        "/auth/reauth", json={"method": "password", "password": PASSWORD}
    )

    assert [r.status_code for r in wrong] == [400, 400, 429]
    assert wrong[0].json()["type"] == "urn:ollamail:problem:reauth-invalid"
    # Locked for the window, even with the right password; the session stays.
    assert correct.status_code == 429
    assert (await db_client.get("/auth/me")).status_code == 200
    rows = await audit_rows(db_session, AuditAction.REAUTH_FAILED)
    assert [row.details for row in rows] == [
        {"method": "password", "reason": "invalid_credentials"}
    ] * 2


async def test_authenticator_code_confirms(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _signed_in(db_client, db_session)
    secret, _ = await enable_totp(db_client)
    await _age_sessions(db_session)

    options = (await db_client.get("/auth/reauth")).json()
    confirmed = await db_client.post(
        "/auth/reauth", json={"method": "totp", "code": code_at(secret)}
    )
    # The same time step cannot be used twice.
    replay = await db_client.post("/auth/reauth", json={"method": "totp", "code": code_at(secret)})
    created = await db_client.post("/auth/mfa/recovery-codes")

    assert options["methods"] == ["password", "totp", "signin"]
    assert confirmed.status_code == 200, confirmed.text
    assert replay.status_code == 400
    assert replay.json()["type"] == "urn:ollamail:problem:mfa-invalid"
    assert created.status_code == 200


async def test_totp_needs_an_active_authenticator(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _signed_in(db_client, db_session)

    response = await db_client.post("/auth/reauth", json={"method": "totp", "code": "123456"})
    mismatched = await db_client.post("/auth/reauth", json={"method": "totp", "password": "x"})

    assert response.status_code == 409
    assert response.json()["type"] == "urn:ollamail:problem:reauth-unavailable"
    assert mismatched.status_code == 422


async def test_passkey_confirms(db_client: AsyncClient, db_session: AsyncSession) -> None:
    await _signed_in(db_client, db_session)
    authenticator, created = await add_passkey(db_client)
    await _age_sessions(db_session)

    methods = (await db_client.get("/auth/reauth")).json()["methods"]
    options = await db_client.post("/auth/reauth/passkey/options")
    confirmed = await db_client.post(
        "/auth/reauth/passkey", json={"credential": authenticator.get(options.json())}
    )
    removed = await db_client.delete(f"/auth/mfa/passkeys/{created['passkey']['id']}")

    assert methods == ["password", "webauthn", "signin"]
    assert options.status_code == 200
    assert confirmed.status_code == 200, confirmed.text
    assert removed.status_code == 204
    assert (await db_session.scalars(select(PendingLogin))).all() == []


async def test_passkey_challenge_is_single_use(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _signed_in(db_client, db_session)
    authenticator, _ = await add_passkey(db_client)
    await _age_sessions(db_session)
    options = (await db_client.post("/auth/reauth/passkey/options")).json()
    assertion = authenticator.get(options)
    assert (await db_client.post("/auth/reauth/passkey", json={"credential": assertion})).is_success
    await _age_sessions(db_session)

    replay = await db_client.post("/auth/reauth/passkey", json={"credential": assertion})

    assert replay.status_code == 400
    rows = await audit_rows(db_session, AuditAction.REAUTH_FAILED)
    assert [row.details for row in rows] == [{"method": "webauthn", "reason": "invalid_passkey"}]


async def test_reauth_needs_a_session(db_client: AsyncClient) -> None:
    assert (await db_client.get("/auth/reauth")).status_code == 401
    response = await db_client.post("/auth/reauth", json={"method": "password", "password": "x"})
    assert response.status_code == 401


async def test_recent_confirmation_allows_the_account_deletion(
    db_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    settings.privacy.self_delete_enabled = True
    await _signed_in(db_client, db_session)
    await _age_sessions(db_session)
    await db_client.post("/auth/reauth", json={"method": "password", "password": PASSWORD})

    response = await db_client.request("DELETE", "/privacy/account", json={"confirm_email": EMAIL})

    assert response.status_code == 204, response.text
    assert await db_session.scalar(select(User.id).where(User.email == EMAIL)) is None


# -- SSO accounts --------------------------------------------------------------------------


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


async def test_sso_accounts_sign_in_again_at_their_provider(
    app_client: tuple[FastAPI, AsyncClient], db_session: AsyncSession
) -> None:
    app, client = app_client
    app.state.auth_providers.register(FakeRedirectProvider())
    await _signed_in(client, db_session)
    # Turn it into an SSO-only account whose session came from the provider.
    await db_session.execute(update(Identity).values(password_hash=None))
    await db_session.execute(update(AuthSession).values(provider="oidc:test"))
    await _age_sessions(db_session)

    options = (await client.get("/auth/reauth")).json()
    password = await client.post("/auth/reauth", json={"method": "password", "password": PASSWORD})

    assert options["methods"] == ["sso", "signin"]
    assert options["provider_display_name"] == "Test IdP"
    assert options["login_path"] == "/auth/test/login"
    assert password.status_code == 409


async def test_unknown_provider_leaves_signing_in_again(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _signed_in(db_client, db_session)
    await db_session.execute(update(Identity).values(password_hash=None))
    await db_session.execute(update(AuthSession).values(provider="ldap:corp"))

    options = (await db_client.get("/auth/reauth")).json()

    assert options["methods"] == ["signin"]
    assert options["login_path"] is None
