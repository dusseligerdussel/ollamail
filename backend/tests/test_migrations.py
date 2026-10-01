import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from alembic import command
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from tests.conftest import TEST_DATABASE_URL, alembic_config

pytestmark = pytest.mark.db


async def _execute_autocommit(url: str, statement: str) -> None:
    engine = create_async_engine(url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    async with engine.connect() as connection:
        await connection.execute(text(statement))
    await engine.dispose()


async def _extensions(url: str) -> set[str]:
    engine = create_async_engine(url, poolclass=NullPool)
    async with engine.connect() as connection:
        result = await connection.execute(text("SELECT extname FROM pg_extension"))
        names = {row[0] for row in result}
    await engine.dispose()
    return names


@pytest.fixture
async def empty_database() -> AsyncIterator[str]:
    """A freshly created, empty database, dropped after the test."""
    name = f"ollamail_migrations_{uuid.uuid4().hex[:12]}"
    try:
        await _execute_autocommit(TEST_DATABASE_URL, f'CREATE DATABASE "{name}"')
    except Exception as exc:
        pytest.skip(f"cannot create a scratch database ({type(exc).__name__})")
    url = make_url(TEST_DATABASE_URL).set(database=name).render_as_string(hide_password=False)
    yield url
    await _execute_autocommit(TEST_DATABASE_URL, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


async def test_upgrade_and_downgrade_on_empty_database(empty_database: str) -> None:
    config = alembic_config(empty_database)

    # env.py runs its own event loop, so Alembic must not run inside this one.
    await asyncio.to_thread(command.upgrade, config, "head")
    assert "vector" in await _extensions(empty_database)

    await asyncio.to_thread(command.downgrade, config, "base")
    assert "vector" not in await _extensions(empty_database)

    await asyncio.to_thread(command.upgrade, config, "head")
    assert "vector" in await _extensions(empty_database)
