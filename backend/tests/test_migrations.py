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


async def test_sort_date_is_backfilled_in_batches(empty_database: str) -> None:
    """``add_message_sort_date`` fills existing rows (more than one batch) and the trigger
    keeps the column up to date afterwards."""
    config = alembic_config(empty_database)
    await asyncio.to_thread(command.upgrade, config, "1efbd562bd72")
    engine = create_async_engine(empty_database, poolclass=NullPool)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO users (id, email, display_name, role, language, timezone)"
                " VALUES (gen_random_uuid(), 'perf@example.org', 'Test', 'user', 'en', 'UTC')"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO mail_mailboxes (id, type, display_name, address, owner_user_id)"
                " SELECT gen_random_uuid(), 'imap', 'Test', 'test@example.org', id FROM users"
            )
        )
        # 25,001 messages (three batches): received date, only a sent date, neither.
        await connection.execute(
            text(
                "INSERT INTO mail_messages (id, mailbox_id, remote_ref, subject, body_text,"
                " body_main, size, received_at, sent_at, created_at)"
                " SELECT gen_random_uuid(), (SELECT id FROM mail_mailboxes), 'ref-' || g, '',"
                " '', '', 0,"
                " CASE WHEN g % 3 = 0 THEN timestamptz '2026-01-01' + g * interval '1 s' END,"
                " CASE WHEN g % 3 <> 2 THEN timestamptz '2025-01-01' + g * interval '1 s' END,"
                " timestamptz '2024-01-01' + g * interval '1 s'"
                " FROM generate_series(1, 25001) AS g"
            )
        )
    await engine.dispose()

    await asyncio.to_thread(command.upgrade, config, "head")

    engine = create_async_engine(empty_database, poolclass=NullPool)
    async with engine.begin() as connection:
        wrong = await connection.scalar(
            text(
                "SELECT count(*) FROM mail_messages"
                " WHERE sort_date IS DISTINCT FROM coalesce(received_at, sent_at, created_at)"
            )
        )
        assert wrong == 0
        changed = await connection.scalar(
            text(
                "UPDATE mail_messages SET received_at = timestamptz '2030-01-01'"
                " WHERE remote_ref = 'ref-2' RETURNING sort_date"
            )
        )
        assert changed is not None and changed.year == 2030
    await engine.dispose()
