"""Login with TOTP and recovery codes: no session before the second factor, time window,
replay protection, attempt limits and lockout."""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditAction
from app.auth.mfa.models import PendingLogin, TotpFactor
from app.auth.mfa.pending import MAX_ATTEMPTS, PENDING_COOKIE
from app.auth.sessions import SESSION_COOKIE
from app.core.config import Settings
from tests.audit.conftest import audit_rows
from tests.auth.conftest import login, make_local_user
from tests.auth.mfa.conftest import code_at, enable_totp, password_step

pytestmark = pytest.mark.db

EMAIL = "erika@example.org"


async def _user_with_totp(client: AsyncClient, db: AsyncSession) -> tuple[str, list[str]]:
    await make_local_user(db, EMAIL)
    assert (await login(client, EMAIL)).status_code == 200
    secret, codes = await enable_totp(client)
    await client.post("/auth/logout")
    return secret, codes


async def test_password_alone_does_not_sign_in(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _user_with_totp(db_client, db_session)

    response = await password_step(db_client, EMAIL)

    body = response.json()
    assert body["status"] == "mfa_required"
    assert body["methods"] == ["totp", "recovery"]
    assert SESSION_COOKIE not in db_client.cookies
    cookie = next(
        c for c in response.headers.get_list("set-cookie") if c.startswith(f"{PENDING_COOKIE}=")
    ).lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "secure" in cookie
    assert (await db_client.get("/auth/me")).status_code == 401
    # The pending state does not open the account endpoints either.
    assert (await db_client.get("/auth/mfa")).status_code == 401
    assert (await db_client.post("/auth/mfa/recovery-codes")).status_code == 401


async def test_totp_completes_the_login_once(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    secret, _ = await _user_with_totp(db_client, db_session)
    await password_step(db_client, EMAIL)

    wrong = await db_client.post("/auth/mfa/verify", json={"method": "totp", "code": "000000"})
    code = code_at(secret)
    right = await db_client.post("/auth/mfa/verify", json={"method": "totp", "code": code})

    assert wrong.status_code == 401
    assert wrong.json()["type"] == "urn:ollamail:problem:mfa-invalid"
    assert right.status_code == 200, right.text
    assert right.json()["email"] == EMAIL
    assert (await db_client.get("/auth/me")).status_code == 200
    assert (await db_session.scalars(select(PendingLogin))).all() == []
    # The pending login is used up, and the same code is not accepted a second time.
    await db_client.post("/auth/logout")
    await password_step(db_client, EMAIL)
    replay = await db_client.post("/auth/mfa/verify", json={"method": "totp", "code": code})
    assert replay.status_code == 401
    failed = await audit_rows(db_session, AuditAction.LOGIN_FAILED)
    assert [row.details["reason"] for row in failed] == ["invalid_code", "invalid_code"]
    assert failed[0].target_id == right.json()["id"]
    succeeded = await audit_rows(db_session, AuditAction.LOGIN_SUCCEEDED)
    assert succeeded[-1].details == {"provider": "local", "mfa": "totp"}


async def test_totp_time_window(db_client: AsyncClient, db_session: AsyncSession) -> None:
    secret, _ = await _user_with_totp(db_client, db_session)
    await db_session.execute(update(TotpFactor).values(last_used_step=None))
    await password_step(db_client, EMAIL)

    too_old = await db_client.post(
        "/auth/mfa/verify", json={"method": "totp", "code": code_at(secret, -3)}
    )
    previous = await db_client.post(
        "/auth/mfa/verify", json={"method": "totp", "code": code_at(secret, -1)}
    )

    assert too_old.status_code == 401
    assert previous.status_code == 200


async def test_recovery_codes_work_once(db_client: AsyncClient, db_session: AsyncSession) -> None:
    _, codes = await _user_with_totp(db_client, db_session)
    assert len(codes) == 10

    await password_step(db_client, EMAIL)
    used = await db_client.post(
        "/auth/mfa/verify", json={"method": "recovery", "code": codes[0].upper()}
    )
    await db_client.post("/auth/logout")
    await password_step(db_client, EMAIL)
    again = await db_client.post("/auth/mfa/verify", json={"method": "recovery", "code": codes[0]})

    assert used.status_code == 200
    assert again.status_code == 401
    (event,) = [
        row
        for row in await audit_rows(db_session, AuditAction.LOGIN_SUCCEEDED)
        if row.details.get("mfa") == "recovery"
    ]
    assert event.details["recovery_codes_remaining"] == 9


async def test_expired_pending_login_needs_the_password_again(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    secret, _ = await _user_with_totp(db_client, db_session)
    await password_step(db_client, EMAIL)
    await db_session.execute(
        update(PendingLogin).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )

    response = await db_client.post(
        "/auth/mfa/verify", json={"method": "totp", "code": code_at(secret)}
    )

    assert response.status_code == 401
    assert response.json()["type"] == "urn:ollamail:problem:mfa-expired"
    assert SESSION_COOKIE not in db_client.cookies


async def test_pending_login_is_bound_to_its_browser(
    db_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    secret, _ = await _user_with_totp(db_client, db_session)
    await password_step(db_client, EMAIL)
    # Another browser (or a guessed token) has no access to this login.
    db_client.cookies.clear()
    db_client.cookies.set(PENDING_COOKIE, "forged-token")

    response = await db_client.post(
        "/auth/mfa/verify", json={"method": "totp", "code": code_at(secret)}
    )

    assert response.status_code == 401
    assert response.json()["type"] == "urn:ollamail:problem:mfa-expired"


async def test_too_many_wrong_codes_drop_the_pending_login(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    secret, _ = await _user_with_totp(db_client, db_session)
    await password_step(db_client, EMAIL)

    statuses = [
        (
            await db_client.post("/auth/mfa/verify", json={"method": "totp", "code": "000000"})
        ).json()["type"]
        for _ in range(MAX_ATTEMPTS)
    ]
    after = await db_client.post(
        "/auth/mfa/verify", json={"method": "totp", "code": code_at(secret)}
    )

    assert statuses[:-1] == ["urn:ollamail:problem:mfa-invalid"] * (MAX_ATTEMPTS - 1)
    assert statuses[-1] == "urn:ollamail:problem:mfa-expired"
    assert after.status_code == 401
    assert (await db_session.scalars(select(PendingLogin))).all() == []


async def test_second_factor_is_locked_across_logins(
    db_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    secret, _ = await _user_with_totp(db_client, db_session)
    limit = settings.auth.login_max_attempts
    statuses = []
    for _ in range(limit + 1):
        await password_step(db_client, EMAIL)
        response = await db_client.post(
            "/auth/mfa/verify", json={"method": "totp", "code": "000000"}
        )
        statuses.append(response.status_code)
    await password_step(db_client, EMAIL)
    locked = await db_client.post(
        "/auth/mfa/verify", json={"method": "totp", "code": code_at(secret)}
    )

    assert statuses == [401] * limit + [429]
    assert locked.status_code == 429
    assert locked.json()["retry_after"] > 0
    reasons = [
        row.details["reason"] for row in await audit_rows(db_session, AuditAction.LOGIN_FAILED)
    ]
    assert reasons.count("locked") == 2


async def test_cancel_drops_the_pending_login(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _user_with_totp(db_client, db_session)
    await password_step(db_client, EMAIL)

    response = await db_client.post("/auth/mfa/cancel")

    assert response.status_code == 204
    assert (await db_session.scalars(select(PendingLogin))).all() == []


async def test_accounts_without_factor_sign_in_directly(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await make_local_user(db_session, EMAIL)

    response = await login(db_client, EMAIL)

    assert response.status_code == 200
    assert SESSION_COOKIE in db_client.cookies
