"""Invitations and self-registration run through the same steps as the login (#144)."""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.sessions import SESSION_COOKIE
from app.core.config import Settings
from app.users.models import UserRole
from tests.auth.conftest import PASSWORD, login, make_local_user
from tests.auth.mfa.conftest import code_at, enable_totp

pytestmark = pytest.mark.db

ADMIN = "admin@example.org"
INVITED = "max@example.org"


async def _admin(client: AsyncClient, db: AsyncSession, enforcement: str | None) -> None:
    """A signed-in admin with TOTP, optionally enforcing 2FA."""
    await make_local_user(db, ADMIN, role=UserRole.ADMIN)
    assert (await login(client, ADMIN)).status_code == 200
    await enable_totp(client)
    if enforcement is not None:
        response = await client.patch("/admin/auth/settings", json={"mfa_enforcement": enforcement})
        assert response.status_code == 200, response.text


async def _invite(client: AsyncClient, role: str) -> str:
    response = await client.post(
        "/users/invitations",
        json={"email": INVITED, "display_name": "Max Muster", "role": role},
    )
    assert response.status_code == 201, response.text
    url: str = response.json()["invite_url"]
    await client.post("/auth/logout")
    return url.partition("#")[2]


async def _accept(client: AsyncClient, token: str) -> dict[str, object]:
    response = await client.post(
        "/auth/invitations/accept", json={"token": token, "password": PASSWORD}
    )
    assert response.status_code == 202, response.text
    body: dict[str, object] = response.json()
    return body


@pytest.mark.parametrize(("enforcement", "role"), [("all", "user"), ("admins", "admin")])
async def test_invited_account_sets_up_a_factor_before_the_session(
    db_client: AsyncClient, db_session: AsyncSession, enforcement: str, role: str
) -> None:
    await _admin(db_client, db_session, enforcement)
    token = await _invite(db_client, role)

    challenge = await _accept(db_client, token)
    me_before = await db_client.get("/auth/me")
    # The password is stored; signing in again ends at the same step.
    second_login = await login(db_client, INVITED)
    secret = (await db_client.post("/auth/mfa/totp/setup")).json()["secret"]
    confirm = await db_client.post("/auth/mfa/totp/confirm", json={"code": code_at(secret)})

    assert challenge["status"] == "mfa_enrollment_required"
    assert me_before.status_code == 401
    assert second_login.json()["status"] == "mfa_enrollment_required"
    assert confirm.status_code == 200, confirm.text
    assert confirm.json()["user"]["email"] == INVITED
    assert SESSION_COOKIE in db_client.cookies
    assert (await db_client.get("/auth/me")).json()["email"] == INVITED


async def test_invitation_without_enforcement_signs_in_directly(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _admin(db_client, db_session, "admins")
    token = await _invite(db_client, "user")

    response = await db_client.post(
        "/auth/invitations/accept", json={"token": token, "password": PASSWORD}
    )

    assert response.status_code == 200, response.text
    assert (await db_client.get("/auth/me")).json()["email"] == INVITED


async def test_registration_under_enforced_2fa_sets_up_a_factor_first(
    db_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    settings.auth.local_registration = True
    await _admin(db_client, db_session, "all")
    await db_client.post("/auth/logout")

    response = await db_client.post(
        "/auth/register",
        json={"email": "new@example.org", "display_name": "New User", "password": PASSWORD},
    )
    me_before = await db_client.get("/auth/me")
    secret = (await db_client.post("/auth/mfa/totp/setup")).json()["secret"]
    confirm = await db_client.post("/auth/mfa/totp/confirm", json={"code": code_at(secret)})

    assert response.status_code == 202, response.text
    assert response.json()["status"] == "mfa_enrollment_required"
    assert me_before.status_code == 401
    assert confirm.status_code == 200, confirm.text
    assert (await db_client.get("/auth/me")).json()["email"] == "new@example.org"


async def test_registration_without_enforcement_signs_in(
    db_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    settings.auth.local_registration = True
    await _admin(db_client, db_session, "admins")
    await db_client.post("/auth/logout")

    response = await db_client.post(
        "/auth/register",
        json={"email": "new@example.org", "display_name": "New User", "password": PASSWORD},
    )

    assert response.status_code == 201, response.text
    assert (await db_client.get("/auth/me")).json()["email"] == "new@example.org"


async def test_invitation_in_a_signed_in_browser_ends_that_session(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The enforced set-up must apply to the invited account, not the admin's session."""
    await _admin(db_client, db_session, "all")
    response = await db_client.post(
        "/users/invitations",
        json={"email": INVITED, "display_name": "Max Muster", "role": "user"},
    )
    token = response.json()["invite_url"].partition("#")[2]
    # Still signed in as the admin.
    assert (await db_client.get("/auth/me")).json()["email"] == ADMIN

    await _accept(db_client, token)
    secret = (await db_client.post("/auth/mfa/totp/setup")).json()["secret"]
    confirm = await db_client.post("/auth/mfa/totp/confirm", json={"code": code_at(secret)})

    assert confirm.status_code == 200, confirm.text
    assert confirm.json()["user"]["email"] == INVITED
    assert (await db_client.get("/auth/me")).json()["email"] == INVITED
