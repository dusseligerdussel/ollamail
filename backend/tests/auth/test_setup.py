import asyncio
import io
import json

import pytest
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.auth.setup import log_setup_status, setup_token
from app.core.config import DatabaseSettings, LoggingSettings, SecuritySettings, Settings
from app.core.crypto import generate_key
from app.core.db import Database
from app.core.logging import configure_logging
from app.main import create_app
from app.users.models import User, UserRole
from tests.auth.conftest import PASSWORD, make_local_user
from tests.conftest import api_client

pytestmark = pytest.mark.db


def _setup_body(settings: Settings, **overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "setup_token": setup_token(settings),
        "email": " Admin@Example.org ",
        "display_name": "Admin",
        "password": PASSWORD,
        "language": "de",
        "timezone": "Europe/Berlin",
    }
    body.update(overrides)
    return body


def _scratch_settings(url: str) -> Settings:
    return Settings(
        database=DatabaseSettings.model_validate({"url": url}),
        security=SecuritySettings.model_validate({"secret_key": generate_key()}),
    )


async def test_status_reports_initialization(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    assert (await db_client.get("/setup/status")).json() == {"initialized": False}

    await make_local_user(db_session)

    assert (await db_client.get("/setup/status")).json() == {"initialized": True}


async def test_setup_creates_admin_and_signs_in(
    db_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    response = await db_client.post("/setup", json=_setup_body(settings))

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "admin@example.org"
    assert body["role"] == "admin"
    assert (body["language"], body["timezone"]) == ("de", "Europe/Berlin")
    me = await db_client.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["id"] == body["id"]


async def test_setup_after_initialization_is_409(
    db_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    assert (await db_client.post("/setup", json=_setup_body(settings))).status_code == 201

    again = await db_client.post("/setup", json=_setup_body(settings, email="second@example.org"))

    assert again.status_code == 409
    assert again.json()["type"] == "urn:ollamail:problem:already-initialized"
    assert await db_session.scalar(select(func.count()).select_from(User)) == 1


async def test_setup_requires_the_token(
    db_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    response = await db_client.post("/setup", json=_setup_body(settings, setup_token="guess"))

    assert response.status_code == 403
    assert await db_session.scalar(select(func.count()).select_from(User)) == 0


async def test_setup_attempts_are_rate_limited_per_ip(
    db_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    settings.auth.ip_max_attempts = 2
    for _ in range(2):
        guess = await db_client.post("/setup", json=_setup_body(settings, setup_token="guess"))
        assert guess.status_code == 403

    # Blocked before the token check, even with the right token.
    throttled = await db_client.post("/setup", json=_setup_body(settings))

    assert throttled.status_code == 429
    assert throttled.json()["type"] == "urn:ollamail:problem:too-many-attempts"
    assert await db_session.scalar(select(func.count()).select_from(User)) == 0


async def test_configured_setup_token_is_used(settings: Settings) -> None:
    settings.security.setup_token = SecretStr("from-the-environment")

    assert setup_token(settings) == "from-the-environment"


def test_derived_setup_token_is_stable_and_key_bound(settings: Settings) -> None:
    other = Settings(security=settings.security.model_copy(update={"secret_key": None}))

    assert setup_token(settings) == setup_token(settings)
    assert len(setup_token(settings)) == 24
    assert setup_token(settings) != setup_token(other)


async def test_setup_enforces_password_policy(
    db_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    response = await db_client.post("/setup", json=_setup_body(settings, password="short"))

    assert response.status_code == 422
    assert response.json()["min_length"] == settings.auth.password_min_length
    assert await db_session.scalar(select(func.count()).select_from(User)) == 0


async def test_setup_rejects_invalid_input_without_echo(
    db_client: AsyncClient, settings: Settings
) -> None:
    response = await db_client.post(
        "/setup", json=_setup_body(settings, email="not-an-address", timezone="Mars/Olympus")
    )

    assert response.status_code == 422
    assert "not-an-address" not in response.text
    assert {tuple(error["loc"]) for error in response.json()["errors"]} == {
        ("body", "email"),
        ("body", "timezone"),
    }


async def test_parallel_setup_creates_exactly_one_admin(scratch_database: str) -> None:
    settings = _scratch_settings(scratch_database)
    app = create_app(settings)
    clients = [api_client(app) for _ in range(5)]
    try:
        responses = await asyncio.gather(
            *(
                client.post("/setup", json=_setup_body(settings, email=f"admin{i}@example.org"))
                for i, client in enumerate(clients)
            )
        )
    finally:
        for client in clients:
            await client.aclose()
        await app.state.database.dispose()

    assert sorted(response.status_code for response in responses) == [201, 409, 409, 409, 409]
    engine = create_async_engine(scratch_database, poolclass=NullPool)
    async with engine.connect() as connection:
        roles = (await connection.execute(select(User.role))).scalars().all()
    await engine.dispose()
    assert roles == [UserRole.ADMIN]


async def _setup_log(settings: Settings) -> list[dict[str, object]]:
    stream = io.StringIO()
    configure_logging(LoggingSettings(format="json"), stream=stream)
    database = Database(settings.database)
    try:
        await log_setup_status(database, settings)
    finally:
        await database.dispose()
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


async def test_pending_setup_logs_the_setup_code(scratch_database: str) -> None:
    settings = _scratch_settings(scratch_database)

    records = await _setup_log(settings)

    pending = [r for r in records if r["event"] == "setup_pending"]
    assert pending[0]["setup_code"] == setup_token(settings)


async def test_configured_token_is_not_logged(scratch_database: str) -> None:
    settings = _scratch_settings(scratch_database)
    settings.security.setup_token = SecretStr("from-the-environment")

    records = await _setup_log(settings)

    assert [r["event"] for r in records] == ["setup_pending"]
    assert "from-the-environment" not in json.dumps(records)


async def test_nothing_is_logged_once_initialized(scratch_database: str) -> None:
    settings = _scratch_settings(scratch_database)
    database = Database(settings.database)
    async with database.sessionmaker() as session:
        await make_local_user(session)
    await database.dispose()

    assert await _setup_log(settings) == []


async def test_unreachable_database_is_reported() -> None:
    url = "postgresql+asyncpg://nobody:nothing@127.0.0.1:1/none"
    settings = Settings(database=DatabaseSettings.model_validate({"url": url}))

    records = await _setup_log(settings)

    assert [r["event"] for r in records] == ["setup_status_unknown"]
