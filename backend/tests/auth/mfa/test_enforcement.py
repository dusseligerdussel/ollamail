"""Enforced 2FA (admin setting), account management and SSO-only accounts."""

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditAction
from app.auth.mfa.models import PendingLogin, RecoveryCode, TotpFactor
from app.auth.models import Identity
from app.auth.sessions import SESSION_COOKIE
from app.users.models import UserRole
from tests.audit.conftest import audit_rows
from tests.auth.conftest import login, make_local_user
from tests.auth.mfa.authenticator import SoftAuthenticator
from tests.auth.mfa.conftest import ORIGIN, code_at, enable_totp, password_step

pytestmark = pytest.mark.db

ADMIN = "admin@example.org"
USER = "erika@example.org"


async def _enforce(client: AsyncClient, value: str) -> dict[str, object]:
    response = await client.patch("/admin/auth/settings", json={"mfa_enforcement": value})
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


async def _admin_enforcing(client: AsyncClient, db: AsyncSession, value: str) -> None:
    await make_local_user(db, ADMIN, role=UserRole.ADMIN)
    assert (await login(client, ADMIN)).status_code == 200
    # The admin protects their own account first.
    await enable_totp(client)
    assert (await _enforce(client, value))["mfa_enforcement"] == value
    await client.post("/auth/logout")


async def test_admin_setting_is_audited_and_admin_only(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await make_local_user(db_session, USER)
    assert (await login(db_client, USER)).status_code == 200
    forbidden = await db_client.patch("/admin/auth/settings", json={"mfa_enforcement": "all"})
    await db_client.post("/auth/logout")
    await _admin_enforcing(db_client, db_session, "admins")

    assert forbidden.status_code == 403
    changes = [
        row.details
        for row in await audit_rows(db_session, AuditAction.IDP_CONFIG_CHANGED)
        if row.details.get("kind") == "mfa"
    ]
    assert changes == [{"kind": "mfa", "change": "admins"}]


async def test_enforced_users_set_up_a_factor_before_the_first_session(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _admin_enforcing(db_client, db_session, "all")
    await make_local_user(db_session, USER)

    challenge = await password_step(db_client, USER)
    me_before = await db_client.get("/auth/me")
    setup = await db_client.post("/auth/mfa/totp/setup")
    secret = setup.json()["secret"]
    confirm = await db_client.post("/auth/mfa/totp/confirm", json={"code": code_at(secret)})

    assert challenge.json()["status"] == "mfa_enrollment_required"
    assert challenge.json()["methods"] == ["webauthn", "totp"]
    assert me_before.status_code == 401
    assert setup.status_code == 200
    assert confirm.status_code == 200, confirm.text
    body = confirm.json()
    assert body["user"]["email"] == USER
    assert len(body["recovery_codes"]) == 10
    assert SESSION_COOKIE in db_client.cookies
    assert (await db_client.get("/auth/me")).json()["email"] == USER
    assert (await db_session.scalars(select(PendingLogin))).all() == []


async def test_enforced_enrolment_with_a_passkey(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _admin_enforcing(db_client, db_session, "all")
    await make_local_user(db_session, USER)
    await password_step(db_client, USER)

    options = (await db_client.post("/auth/mfa/passkeys/options")).json()
    response = await db_client.post(
        "/auth/mfa/passkeys",
        json={"name": "Laptop", "credential": SoftAuthenticator(origin=ORIGIN).create(options)},
    )

    assert response.status_code == 201, response.text
    assert response.json()["user"]["email"] == USER
    assert (await db_client.get("/auth/me")).status_code == 200


async def test_enforcement_for_admins_leaves_users_alone(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _admin_enforcing(db_client, db_session, "admins")
    await make_local_user(db_session, USER)

    assert (await login(db_client, USER)).status_code == 200


async def test_the_last_factor_cannot_be_removed_while_enforced(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _admin_enforcing(db_client, db_session, "admins")
    secret = (await db_session.scalar(select(TotpFactor.secret))) or ""
    await password_step(db_client, ADMIN)
    assert (
        await db_client.post("/auth/mfa/verify", json={"method": "totp", "code": code_at(secret)})
    ).status_code == 200

    response = await db_client.delete("/auth/mfa/totp")

    assert response.status_code == 409
    assert response.json()["type"] == "urn:ollamail:problem:mfa-required"
    assert (await db_client.get("/auth/mfa")).json()["totp"] is True


async def test_totp_setup_cannot_replace_an_active_one(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await make_local_user(db_session, USER)
    assert (await login(db_client, USER)).status_code == 200
    await enable_totp(db_client)

    again = await db_client.post("/auth/mfa/totp/setup")
    regenerated = await db_client.post("/auth/mfa/recovery-codes")
    removed = await db_client.delete("/auth/mfa/totp")

    assert again.status_code == 409
    assert regenerated.status_code == 200
    assert len(regenerated.json()["codes"]) == 10
    assert removed.status_code == 204
    assert (await db_session.scalars(select(RecoveryCode))).all() == []
    assert (await db_client.post("/auth/mfa/recovery-codes")).status_code == 409


async def test_wrong_confirmation_code_keeps_totp_off(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await make_local_user(db_session, USER)
    assert (await login(db_client, USER)).status_code == 200
    await db_client.post("/auth/mfa/totp/setup")

    response = await db_client.post("/auth/mfa/totp/confirm", json={"code": "000000"})

    assert response.status_code == 401
    status = (await db_client.get("/auth/mfa")).json()
    assert status["totp"] is False
    assert status["recovery_codes_remaining"] == 0
    # The secret is stored encrypted.
    raw = (await db_session.execute(text("SELECT secret FROM auth_mfa_totp"))).scalar_one()
    plain = await db_session.scalar(select(TotpFactor.secret))
    assert plain is not None and plain not in raw


async def test_sso_accounts_have_no_local_second_factor(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_local_user(db_session, USER)
    assert (await login(db_client, USER)).status_code == 200
    # The account loses its password (SSO only).
    identity = await db_session.scalar(select(Identity).where(Identity.user_id == user.id))
    assert identity is not None
    identity.password_hash = None
    await db_session.commit()

    status = (await db_client.get("/auth/mfa")).json()
    setup = await db_client.post("/auth/mfa/totp/setup")

    assert status["available"] is False
    assert setup.status_code == 409
    assert setup.json()["type"] == "urn:ollamail:problem:mfa-unavailable"
