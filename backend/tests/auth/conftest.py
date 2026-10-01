"""Fixtures for auth tests: cheap password hashing, users, sign-in helpers."""

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from argon2 import PasswordHasher, Type
from httpx import AsyncClient, Response
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.auth import passwords
from app.auth.passwords import hash_password
from app.users.models import User, UserRole
from app.users.service import add_local_user
from tests.conftest import TEST_DATABASE_URL, alembic_config

PASSWORD = "correct horse battery staple"


@pytest.fixture(autouse=True)
def _cheap_hashing() -> Iterator[None]:
    """Argon2id with minimal cost; the production parameters make tests slow."""
    original = passwords._hasher
    passwords.configure_hasher(
        PasswordHasher(time_cost=1, memory_cost=8, parallelism=1, type=Type.ID)
    )
    yield
    passwords.configure_hasher(original)


async def make_local_user(
    session: AsyncSession,
    email: str = "erika@example.org",
    *,
    role: UserRole = UserRole.USER,
    password: str = PASSWORD,
) -> User:
    user = await add_local_user(
        session,
        email=email,
        display_name="Erika Mustermann",
        password_hash=await hash_password(password),
        role=role,
    )
    await session.commit()
    return user


async def login(client: AsyncClient, email: str, password: str = PASSWORD) -> Response:
    return await client.post("/auth/login", json={"email": email, "password": password})


async def _execute_autocommit(url: str, statement: str) -> None:
    engine = create_async_engine(url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    async with engine.connect() as connection:
        await connection.execute(text(statement))
    await engine.dispose()


@pytest.fixture
async def scratch_database(migrated_database: str) -> AsyncIterator[str]:
    """A separate, migrated database for tests that need real commits (races, CLI)."""
    name = f"ollamail_auth_{uuid.uuid4().hex[:12]}"
    try:
        await _execute_autocommit(TEST_DATABASE_URL, f'CREATE DATABASE "{name}"')
    except Exception as exc:
        pytest.skip(f"cannot create a scratch database ({type(exc).__name__})")
    url = make_url(TEST_DATABASE_URL).set(database=name).render_as_string(hide_password=False)
    try:
        await asyncio.to_thread(_upgrade, url)
        yield url
    finally:
        await _execute_autocommit(
            TEST_DATABASE_URL, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'
        )


def _upgrade(url: str) -> None:
    from alembic import command

    command.upgrade(alembic_config(url), "head")
