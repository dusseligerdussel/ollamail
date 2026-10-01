import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import DatabaseSettings, Settings
from app.core.health import register_readiness_check
from app.main import create_app

UNREACHABLE_DB = "postgresql+asyncpg://ollamail:ollamail@127.0.0.1:1/ollamail"


async def test_healthz_returns_ok(client: AsyncClient) -> None:
    response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readyz_returns_503_when_database_unreachable() -> None:
    settings = Settings(database=DatabaseSettings.model_validate({"url": UNREACHABLE_DB}))
    app = create_app(settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/readyz")
    await app.state.database.dispose()

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "checks": {"database": "failed"}}


async def test_readyz_reports_registered_checks(settings: Settings) -> None:
    settings = Settings(database=DatabaseSettings.model_validate({"url": UNREACHABLE_DB}))
    app = create_app(settings)
    app.state.readiness.timeout = 0.05

    async def ok() -> None:
        return None

    async def hangs() -> None:
        await asyncio.sleep(10)

    register_readiness_check(app, "queue", ok)
    register_readiness_check(app, "llm", hangs)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/readyz")
    await app.state.database.dispose()

    assert response.status_code == 503
    assert response.json()["checks"] == {"database": "failed", "queue": "ok", "llm": "failed"}


def test_duplicate_readiness_check_is_rejected(settings: Settings) -> None:
    app = create_app(settings)

    async def ok() -> None:
        return None

    with pytest.raises(ValueError):
        register_readiness_check(app, "database", ok)


@pytest.mark.db
async def test_readyz_returns_200_when_database_reachable(
    client: AsyncClient, migrated_database: str
) -> None:
    response = await client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"database": "ok"}}
