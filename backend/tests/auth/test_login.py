from datetime import UTC, datetime, timedelta

import pytest
from argon2 import PasswordHasher, Type
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import passwords
from app.auth.models import LOCAL_PROVIDER, AuthSession, Identity, rate_limits
from app.auth.sessions import SESSION_COOKIE
from app.core.config import Settings
from app.users.models import User
from tests.auth.conftest import PASSWORD, login, make_local_user

pytestmark = pytest.mark.db


def _set_cookies(response_headers: list[str], name: str) -> str:
    return next(value for value in response_headers if value.startswith(f"{name}="))


async def test_login_sets_secure_session_cookie(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_local_user(db_session)

    response = await login(db_client, " Erika@Example.org ")

    assert response.status_code == 200
    assert response.json()["id"] == str(user.id)
    cookie = _set_cookies(response.headers.get_list("set-cookie"), SESSION_COOKIE).lower()
    assert "httponly" in cookie
    assert "secure" in cookie
    assert "samesite=lax" in cookie
    assert "path=/" in cookie
    me = await db_client.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == "erika@example.org"


async def test_session_token_is_stored_hashed(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await make_local_user(db_session)
    await login(db_client, "erika@example.org")
    token = db_client.cookies[SESSION_COOKIE]

    stored = (await db_session.scalars(select(AuthSession.token_hash))).all()

    assert len(stored) == 1
    assert token.encode() not in stored[0]
    assert len(stored[0]) == 32


async def test_wrong_password_and_unknown_user_look_the_same(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await make_local_user(db_session)

    wrong = await login(db_client, "erika@example.org", "wrong password!")
    unknown = await login(db_client, "nobody@example.org")
    garbage = await login(db_client, "no address at all")

    assert wrong.status_code == unknown.status_code == garbage.status_code == 401
    assert wrong.json()["detail"] == unknown.json()["detail"] == garbage.json()["detail"]
    assert SESSION_COOKIE not in db_client.cookies


async def test_account_is_locked_after_too_many_attempts(
    db_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    await make_local_user(db_session)
    await make_local_user(db_session, "max@example.org")
    attempts = settings.auth.login_max_attempts

    statuses = [
        (await login(db_client, "erika@example.org", "wrong password!")).status_code
        for _ in range(attempts)
    ]
    locked = await login(db_client, "erika@example.org")

    assert statuses == [401] * attempts
    assert locked.status_code == 429
    assert locked.json()["type"] == "urn:ollamail:problem:too-many-attempts"
    assert 0 < locked.json()["retry_after"] <= settings.auth.login_window_minutes * 60 + 1
    # Other accounts are not affected.
    assert (await login(db_client, "max@example.org")).status_code == 200


async def test_lockout_ends_with_the_window(
    db_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    await make_local_user(db_session)
    for _ in range(settings.auth.login_max_attempts + 1):
        await login(db_client, "erika@example.org", "wrong password!")
    assert (await login(db_client, "erika@example.org")).status_code == 429

    past = datetime.now(UTC) - timedelta(minutes=settings.auth.login_window_minutes + 1)
    await db_session.execute(update(rate_limits).values(window_start=past))

    assert (await login(db_client, "erika@example.org")).status_code == 200


async def test_unknown_accounts_are_locked_too(db_client: AsyncClient, settings: Settings) -> None:
    for _ in range(settings.auth.login_max_attempts):
        await login(db_client, "nobody@example.org")

    assert (await login(db_client, "nobody@example.org")).status_code == 429


async def test_successful_login_resets_the_counter(
    db_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    await make_local_user(db_session)
    for _ in range(settings.auth.login_max_attempts - 1):
        await login(db_client, "erika@example.org", "wrong password!")
    assert (await login(db_client, "erika@example.org")).status_code == 200

    for _ in range(settings.auth.login_max_attempts - 1):
        await login(db_client, "erika@example.org", "wrong password!")

    assert (await login(db_client, "erika@example.org")).status_code == 200


async def test_rate_limit_keys_contain_no_personal_data(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await login(db_client, "erika@example.org", "wrong password!")

    keys = (await db_session.execute(select(rate_limits.c.key))).scalars().all()

    assert len(keys) == 2
    assert not any("erika" in key or "127.0.0.1" in key for key in keys)


async def test_ip_rate_limit(
    db_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    settings.auth.ip_max_attempts = 3
    for index in range(3):
        await login(db_client, f"user{index}@example.org")

    response = await login(db_client, "another@example.org")

    assert response.status_code == 429


async def test_forged_forwarded_for_does_not_bypass_ip_rate_limit(
    db_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    # An outer proxy appends the real client IP; the client varies the part in front of it.
    settings.auth.ip_max_attempts = 3
    for index in range(3):
        await db_client.post(
            "/auth/login",
            json={"email": f"user{index}@example.org", "password": PASSWORD},
            headers={"X-Forwarded-For": f"6.6.6.{index}, 198.51.100.7"},
        )

    response = await db_client.post(
        "/auth/login",
        json={"email": "another@example.org", "password": PASSWORD},
        headers={"X-Forwarded-For": "6.6.6.99, 198.51.100.7"},
    )

    assert response.status_code == 429


async def test_inactive_user_cannot_sign_in_and_loses_sessions(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_local_user(db_session)
    assert (await login(db_client, "erika@example.org")).status_code == 200

    user.is_active = False
    await db_session.commit()

    assert (await db_client.get("/auth/me")).status_code == 401
    assert (await login(db_client, "erika@example.org")).status_code == 401


async def test_logout_ends_the_session(db_client: AsyncClient, db_session: AsyncSession) -> None:
    await make_local_user(db_session)
    await login(db_client, "erika@example.org")
    token = db_client.cookies[SESSION_COOKIE]

    response = await db_client.post("/auth/logout")

    assert response.status_code == 204
    assert SESSION_COOKIE not in db_client.cookies
    assert await db_session.scalar(select(AuthSession)) is None
    # The old token stays invalid even if a client keeps sending it.
    db_client.cookies.set(SESSION_COOKIE, token)
    assert (await db_client.get("/auth/me")).status_code == 401


async def test_login_replaces_the_previous_session(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await make_local_user(db_session)
    await login(db_client, "erika@example.org")
    first = db_client.cookies[SESSION_COOKIE]

    await login(db_client, "erika@example.org")

    assert db_client.cookies[SESSION_COOKIE] != first
    assert len((await db_session.scalars(select(AuthSession))).all()) == 1


async def test_idle_and_absolute_timeouts(
    db_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    await make_local_user(db_session)
    await login(db_client, "erika@example.org")
    now = datetime.now(UTC)

    idle = timedelta(minutes=settings.auth.session_idle_timeout_minutes + 1)
    await db_session.execute(update(AuthSession).values(last_seen_at=now - idle))
    assert (await db_client.get("/auth/me")).status_code == 401

    await db_session.execute(
        update(AuthSession).values(last_seen_at=now, expires_at=now - timedelta(seconds=1))
    )
    assert (await db_client.get("/auth/me")).status_code == 401

    await db_session.execute(update(AuthSession).values(expires_at=now + timedelta(hours=1)))
    assert (await db_client.get("/auth/me")).status_code == 200


async def test_activity_extends_the_idle_timeout(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await make_local_user(db_session)
    await login(db_client, "erika@example.org")
    earlier = datetime.now(UTC) - timedelta(minutes=30)
    await db_session.execute(update(AuthSession).values(last_seen_at=earlier))

    await db_client.get("/auth/me")

    session = await db_session.scalar(select(AuthSession))
    assert session is not None
    await db_session.refresh(session)
    assert session.last_seen_at > earlier + timedelta(minutes=29)


async def test_outdated_hash_is_upgraded_on_login(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_local_user(db_session)
    identity = await db_session.scalar(select(Identity).where(Identity.user_id == user.id))
    assert identity is not None
    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1, type=Type.ID)
    stronger = PasswordHasher(time_cost=2, memory_cost=16, parallelism=1, type=Type.ID)
    identity.password_hash = weak.hash(PASSWORD)
    await db_session.commit()
    passwords.configure_hasher(stronger)

    assert (await login(db_client, "erika@example.org")).status_code == 200

    await db_session.refresh(identity)
    assert identity.password_hash is not None
    assert not stronger.check_needs_rehash(identity.password_hash)
    assert identity.provider == LOCAL_PROVIDER
    assert identity.last_used_at is not None


async def test_password_hash_is_argon2id(db_session: AsyncSession) -> None:
    user = await make_local_user(db_session)

    identity = await db_session.scalar(select(Identity).where(Identity.user_id == user.id))

    assert identity is not None and identity.password_hash is not None
    assert identity.password_hash.startswith("$argon2id$")
    assert PASSWORD not in identity.password_hash


async def test_deleting_a_user_cascades(db_client: AsyncClient, db_session: AsyncSession) -> None:
    user = await make_local_user(db_session)
    await login(db_client, "erika@example.org")

    await db_session.delete(user)
    await db_session.commit()

    assert await db_session.scalar(select(AuthSession)) is None
    assert await db_session.scalar(select(Identity)) is None
    assert await db_session.scalar(select(User)) is None
