from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.auth import rate_limit
from app.auth.models import AuthSession, rate_limits
from app.auth.sessions import SESSION_COOKIE, purge_expired_sessions, session_active
from app.core.config import Settings
from app.core.db import get_db
from app.main import create_app
from app.users.models import User
from tests.auth.conftest import login, make_local_user
from tests.conftest import api_client

pytestmark = pytest.mark.db


@pytest.fixture
async def second_client(settings: Settings, db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """Another browser of the same user."""
    app: FastAPI = create_app(settings)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    async with api_client(app) as http:
        http.headers["User-Agent"] = "Laptop Browser"
        yield http
    await app.state.database.dispose()


async def _two_sessions(
    db_client: AsyncClient, second_client: AsyncClient, db_session: AsyncSession
) -> None:
    await make_local_user(db_session)
    assert (await login(db_client, "erika@example.org")).status_code == 200
    assert (await login(second_client, "erika@example.org")).status_code == 200


async def test_list_sessions_marks_the_current_one(
    db_client: AsyncClient, second_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _two_sessions(db_client, second_client, db_session)

    response = await db_client.get("/auth/sessions")

    assert response.status_code == 200
    sessions = response.json()
    assert len(sessions) == 2
    assert sum(session["current"] for session in sessions) == 1
    other = next(session for session in sessions if not session["current"])
    assert other["user_agent"] == "Laptop Browser"
    assert other["provider"] == "local"


async def test_revoke_another_session(
    db_client: AsyncClient, second_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _two_sessions(db_client, second_client, db_session)
    sessions = (await db_client.get("/auth/sessions")).json()
    other = next(session for session in sessions if not session["current"])

    response = await db_client.delete(f"/auth/sessions/{other['id']}")

    assert response.status_code == 204
    assert (await second_client.get("/auth/me")).status_code == 401
    assert (await db_client.get("/auth/me")).status_code == 200


async def test_revoking_the_current_session_signs_out(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await make_local_user(db_session)
    await login(db_client, "erika@example.org")
    current = (await db_client.get("/auth/sessions")).json()[0]

    response = await db_client.delete(f"/auth/sessions/{current['id']}")

    assert response.status_code == 204
    assert SESSION_COOKIE not in db_client.cookies
    assert (await db_client.get("/auth/me")).status_code == 401


async def test_cannot_revoke_sessions_of_other_users(
    db_client: AsyncClient, second_client: AsyncClient, db_session: AsyncSession
) -> None:
    await make_local_user(db_session)
    await make_local_user(db_session, "max@example.org")
    await login(db_client, "erika@example.org")
    await login(second_client, "max@example.org")
    foreign = (await second_client.get("/auth/sessions")).json()[0]

    response = await db_client.delete(f"/auth/sessions/{foreign['id']}")

    assert response.status_code == 404
    assert (await second_client.get("/auth/me")).status_code == 200


async def test_sign_out_everywhere_else(
    db_client: AsyncClient, second_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _two_sessions(db_client, second_client, db_session)

    response = await db_client.delete("/auth/sessions")

    assert response.status_code == 204
    assert (await db_client.get("/auth/me")).status_code == 200
    assert (await second_client.get("/auth/me")).status_code == 401


async def test_sign_out_everywhere(
    db_client: AsyncClient, second_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _two_sessions(db_client, second_client, db_session)

    response = await db_client.delete("/auth/sessions", params={"include_current": True})

    assert response.status_code == 204
    assert (await db_client.get("/auth/me")).status_code == 401
    assert (await second_client.get("/auth/me")).status_code == 401
    assert await db_session.scalar(select(func.count()).select_from(AuthSession)) == 0


async def test_session_endpoints_require_authentication(db_client: AsyncClient) -> None:
    assert (await db_client.get("/auth/sessions")).status_code == 401
    assert (await db_client.delete("/auth/sessions")).status_code == 401
    assert (await db_client.get("/auth/me")).status_code == 401


async def test_purge_removes_expired_and_idle_sessions(
    db_client: AsyncClient,
    second_client: AsyncClient,
    db_session: AsyncSession,
    settings: Settings,
) -> None:
    await _two_sessions(db_client, second_client, db_session)
    await login(db_client, "erika@example.org")  # replaces the first session
    sessions = (await db_session.scalars(select(AuthSession))).all()
    idle = timedelta(minutes=settings.auth.session_idle_timeout_minutes + 1)
    await db_session.execute(
        update(AuthSession)
        .where(AuthSession.id == sessions[0].id)
        .values(last_seen_at=datetime.now(UTC) - idle)
    )

    assert await purge_expired_sessions(db_session, settings.auth) == 1
    assert await db_session.scalar(select(func.count()).select_from(AuthSession)) == 1


async def test_session_active_does_not_refresh_the_session(
    db_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await make_local_user(db_session)
    assert (await login(db_client, "erika@example.org")).status_code == 200
    session = await db_session.scalar(select(AuthSession))
    assert session is not None
    seen = datetime.now(UTC) - timedelta(minutes=5)
    await db_session.execute(
        update(AuthSession).where(AuthSession.id == session.id).values(last_seen_at=seen)
    )

    assert await session_active(db_session, settings.auth, session.id)
    stored = await db_session.scalar(
        select(AuthSession.last_seen_at).where(AuthSession.id == session.id)
    )
    assert stored == seen

    # Idle, deactivated and ended sessions are no longer active.
    idle = datetime.now(UTC) - timedelta(minutes=settings.auth.session_idle_timeout_minutes + 1)
    await db_session.execute(
        update(AuthSession).where(AuthSession.id == session.id).values(last_seen_at=idle)
    )
    assert not await session_active(db_session, settings.auth, session.id)
    await db_session.execute(
        update(AuthSession).where(AuthSession.id == session.id).values(last_seen_at=seen)
    )
    await db_session.execute(update(User).where(User.id == user.id).values(is_active=False))
    assert not await session_active(db_session, settings.auth, session.id)
    await db_session.execute(update(User).where(User.id == user.id).values(is_active=True))
    assert await session_active(db_session, settings.auth, session.id)
    assert (await db_client.post("/auth/logout")).status_code == 204
    assert not await session_active(db_session, settings.auth, session.id)


async def test_rate_limit_purge_keeps_current_windows(db_session: AsyncSession) -> None:
    window = timedelta(minutes=15)
    await rate_limit.hit(db_session, "old", window)
    await rate_limit.hit(db_session, "current", window)
    await db_session.execute(
        update(rate_limits)
        .where(rate_limits.c.key == "old")
        .values(window_start=datetime.now(UTC) - timedelta(hours=1))
    )

    assert await rate_limit.purge(db_session, window) == 1
    keys = (await db_session.execute(select(rate_limits.c.key))).scalars().all()
    assert keys == ["current"]


async def test_rate_limit_window_restarts(db_session: AsyncSession) -> None:
    window = timedelta(minutes=15)
    assert (await rate_limit.hit(db_session, "k", window)).count == 1
    assert (await rate_limit.hit(db_session, "k", window)).count == 2
    await db_session.execute(
        update(rate_limits).values(window_start=datetime.now(UTC) - timedelta(minutes=16))
    )

    restarted = await rate_limit.hit(db_session, "k", window)

    assert restarted.count == 1
    assert 890 <= restarted.retry_after() <= 901


async def test_cleanup_job(monkeypatch: pytest.MonkeyPatch, scratch_database: str) -> None:
    from app.auth.tasks import cleanup
    from app.core.config import get_settings
    from app.core.db import Database, bind_process_database

    monkeypatch.setenv("OLLAMAIL_DATABASE_URL", scratch_database)
    get_settings.cache_clear()
    engine = create_async_engine(scratch_database, poolclass=NullPool)
    async with engine.begin() as connection:
        await connection.execute(
            insert(rate_limits).values(
                key="stale", window_start=datetime.now(UTC) - timedelta(days=1), hits=3
            )
        )
    database = Database(get_settings().database)
    try:
        with bind_process_database(database):
            await cleanup.func(timestamp=0)
        async with engine.connect() as connection:
            remaining = await connection.execute(select(func.count()).select_from(rate_limits))
            assert remaining.scalar() == 0
    finally:
        await database.dispose()
        await engine.dispose()
        get_settings.cache_clear()
