"""Deactivated accounts (admin or SCIM ``active=false``, which set ``users.is_active`` to
false) cannot finish a sign-in with a second factor, not even one started before."""

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.sessions import SESSION_COOKIE
from app.users.models import User, UserRole
from tests.auth.conftest import login, make_local_user
from tests.auth.mfa.authenticator import SoftAuthenticator
from tests.auth.mfa.conftest import ORIGIN, add_passkey, code_at, enable_totp, password_step

pytestmark = pytest.mark.db

EMAIL = "erika@example.org"


async def _deactivate(db: AsyncSession) -> None:
    # What app.scim.service does for active=false (besides deleting the sessions).
    await db.execute(update(User).where(User.email == EMAIL).values(is_active=False))
    await db.commit()


async def test_pending_totp_login_fails_after_deactivation(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await make_local_user(db_session, EMAIL)
    assert (await login(db_client, EMAIL)).status_code == 200
    secret, codes = await enable_totp(db_client)
    await db_client.post("/auth/logout")
    await password_step(db_client, EMAIL)

    await _deactivate(db_session)
    by_code = await db_client.post(
        "/auth/mfa/verify", json={"method": "totp", "code": code_at(secret)}
    )
    by_recovery = await db_client.post(
        "/auth/mfa/verify", json={"method": "recovery", "code": codes[0]}
    )
    password = await login(db_client, EMAIL)

    assert by_code.status_code == 401
    assert by_recovery.status_code == 401
    assert password.status_code == 401
    assert SESSION_COOKIE not in db_client.cookies


async def test_passkeys_of_deactivated_users_do_not_sign_in(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await make_local_user(db_session, EMAIL)
    assert (await login(db_client, EMAIL)).status_code == 200
    authenticator, _ = await add_passkey(db_client)
    await db_client.post("/auth/logout")
    await password_step(db_client, EMAIL)
    second_factor_options = (await db_client.post("/auth/mfa/verify/passkey/options")).json()

    await _deactivate(db_session)
    second_factor = await db_client.post(
        "/auth/mfa/verify/passkey",
        json={"credential": authenticator.get(second_factor_options)},
    )
    options = (await db_client.post("/auth/passkey/options")).json()
    passwordless = await db_client.post(
        "/auth/passkey/login", json={"credential": authenticator.get(options)}
    )

    assert second_factor.status_code == 401
    assert passwordless.status_code == 401
    assert SESSION_COOKIE not in db_client.cookies


async def test_enforced_enrolment_stops_after_deactivation(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await make_local_user(db_session, "admin@example.org", role=UserRole.ADMIN)
    assert (await login(db_client, "admin@example.org")).status_code == 200
    await enable_totp(db_client)
    await db_client.patch("/admin/auth/settings", json={"mfa_enforcement": "all"})
    await db_client.post("/auth/logout")
    await make_local_user(db_session, EMAIL)
    await password_step(db_client, EMAIL)
    options = (await db_client.post("/auth/mfa/passkeys/options")).json()

    await _deactivate(db_session)
    setup = await db_client.post("/auth/mfa/totp/setup")
    register = await db_client.post(
        "/auth/mfa/passkeys",
        json={"name": "Laptop", "credential": SoftAuthenticator(origin=ORIGIN).create(options)},
    )

    assert setup.status_code == 401
    assert register.status_code == 401
    assert SESSION_COOKIE not in db_client.cookies
