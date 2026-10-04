"""Shared fixtures.

Tests marked ``db`` need PostgreSQL with pgvector at ``OLLAMAIL_TEST_DATABASE_URL``
(default: the CI database). If it is unreachable they are skipped with a clear message,
so unit tests run anywhere. Set ``OLLAMAIL_TEST_REQUIRE_DB=1`` (CI) to fail instead.

Each ``db_session`` runs inside a transaction that is rolled back after the test;
``session.commit()`` in code under test only releases a savepoint.
"""

import asyncio
import importlib
import os

# Test setting: the scripted servers and the CI services (Dovecot, Mailpit) listen on
# localhost, which mail connections refuse without an allowlist entry
# (app/core/network.py); the same holds for CalDAV export targets. Tests of the check pass
# their own settings.
os.environ.setdefault("OLLAMAIL_MAIL_ALLOWED_INTERNAL_HOSTS", "localhost,127.0.0.0/8,::1")
os.environ.setdefault("OLLAMAIL_TODOS_EXPORT_ALLOWED_INTERNAL_HOSTS", "localhost,127.0.0.0/8,::1")
from collections.abc import AsyncIterator, Callable, Iterator
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.auth.csrf import (
    CSRF_COOKIE,
    CSRF_HEADER,
    SAFE_METHODS,
    csrf_token_valid,
    issue_csrf_token,
)
from app.auth.sessions import SESSION_COOKIE
from app.core.config import DatabaseSettings, LoggingSettings, SecuritySettings, Settings
from app.core.crypto import generate_key
from app.core.db import get_db
from app.core.logging import configure_logging
from app.main import create_app
from app.worker import TASK_MODULES

# Import task modules (and so register their processing steps) before any test isolates
# the step registry; the worker would otherwise import them inside the isolated registry.
for _module in TASK_MODULES:
    importlib.import_module(_module)

BACKEND_DIR = Path(__file__).resolve().parent.parent
TEST_DATABASE_URL = os.environ.get(
    "OLLAMAIL_TEST_DATABASE_URL",
    "postgresql+asyncpg://ollamail:ollamail@localhost:5432/ollamail_test",
)
REQUIRE_DB = os.environ.get("OLLAMAIL_TEST_REQUIRE_DB", "").lower() in {"1", "true", "yes"}


def safe_url(url: str) -> str:
    """URL with the password masked, for messages."""
    return make_url(url).render_as_string(hide_password=True)


async def _probe(url: str) -> str | None:
    engine = create_async_engine(url, poolclass=NullPool, connect_args={"timeout": 3})
    try:
        async with engine.connect():
            return None
    except Exception as exc:
        return type(exc).__name__
    finally:
        await engine.dispose()


def alembic_config(url: str) -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    config.attributes["database_url"] = url
    config.attributes["configure_logger"] = False
    return config


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    db_items = [item for item in items if item.get_closest_marker("db")]
    if not db_items:
        return
    error = asyncio.run(_probe(TEST_DATABASE_URL))
    if error is None:
        return
    message = (
        f"PostgreSQL not reachable at {safe_url(TEST_DATABASE_URL)} ({error}). "
        "Start one (e.g. docker run -p 5432:5432 -e POSTGRES_USER=ollamail "
        "-e POSTGRES_PASSWORD=ollamail -e POSTGRES_DB=ollamail_test pgvector/pgvector:pg16) "
        "or set OLLAMAIL_TEST_DATABASE_URL."
    )
    if REQUIRE_DB:
        raise pytest.UsageError(message)
    skip = pytest.mark.skip(reason=message)
    for item in db_items:
        item.add_marker(skip)


@pytest.fixture(scope="session")
def migrated_database() -> str:
    """Upgrade the test database to the latest revision once per session."""
    command.upgrade(alembic_config(TEST_DATABASE_URL), "head")
    return TEST_DATABASE_URL


@pytest.fixture
async def db_session(migrated_database: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()
    await engine.dispose()


ExtraRoute = tuple[str, Callable[..., Any], list[str]]


@pytest.fixture
def extra_routes() -> list[ExtraRoute]:
    """Override in a test module to mount test-only endpoints on ``db_client``."""
    return []


def api_client(app: FastAPI) -> AsyncClient:
    """HTTPS client that behaves like the frontend: keeps cookies and sends the CSRF
    token (``X-CSRF-Token``) on state-changing requests. Requests that set the header
    themselves are left alone, so CSRF tests can send wrong or missing tokens."""
    settings: Settings = app.state.settings

    async def add_csrf(request: httpx.Request) -> None:
        if request.method in SAFE_METHODS or CSRF_HEADER in request.headers:
            return
        cookies = {
            name: morsel.value
            for name, morsel in SimpleCookie(request.headers.get("cookie", "")).items()
        }
        session_token = cookies.get(SESSION_COOKIE)
        token = cookies.get(CSRF_COOKIE)
        if not csrf_token_valid(settings, token, session_token):
            # What the first GET of the frontend would have received.
            token = issue_csrf_token(settings, session_token)
            cookies[CSRF_COOKIE] = token
            request.headers["cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        assert token is not None
        request.headers[CSRF_HEADER] = token

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(
        transport=transport, base_url="https://test", event_hooks={"request": [add_csrf]}
    )


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database=DatabaseSettings.model_validate({"url": TEST_DATABASE_URL}),
        security=SecuritySettings.model_validate({"secret_key": generate_key()}),
    )


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[AsyncClient]:
    """Client for an app without database overrides (unit-level API tests)."""
    app = create_app(settings)
    async with api_client(app) as http:
        yield http
    await app.state.database.dispose()


@pytest.fixture
async def db_client(
    settings: Settings, db_session: AsyncSession, extra_routes: list[ExtraRoute]
) -> AsyncIterator[AsyncClient]:
    """Client whose ``get_db`` dependency uses the rolled-back test session."""
    app = create_app(settings)
    for route, endpoint, methods in extra_routes:
        app.add_api_route(route, endpoint, methods=methods)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    async with api_client(app) as http:
        yield http
    await app.state.database.dispose()


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """Tests may reconfigure logging; keep the default configuration between tests."""
    yield
    configure_logging(LoggingSettings())
