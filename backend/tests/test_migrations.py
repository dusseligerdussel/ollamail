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
    await asyncio.to_thread(command.upgrade, config, "69dd8258f443")
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


async def test_triage_list_columns_are_backfilled_in_batches(empty_database: str) -> None:
    """``denormalise_triage_list_columns`` copies ``mailbox_id`` and ``sort_date`` of the
    message into existing results (more than one batch); the triggers fill new results and
    follow a changed ``sort_date`` afterwards."""
    config = alembic_config(empty_database)
    await asyncio.to_thread(command.upgrade, config, "93abef19553f")
    engine = create_async_engine(empty_database, poolclass=NullPool)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO users (id, email, display_name, role, language, timezone)"
                " VALUES (gen_random_uuid(), 'triage@example.org', 'Test', 'user', 'en', 'UTC')"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO mail_mailboxes (id, type, display_name, address, owner_user_id)"
                " SELECT gen_random_uuid(), 'imap', 'Test', 'test@example.org', id FROM users"
            )
        )
        # 10,002 messages, all but one triaged (two batches).
        await connection.execute(
            text(
                "INSERT INTO mail_messages (id, mailbox_id, remote_ref, subject, body_text,"
                " body_main, size, received_at)"
                " SELECT gen_random_uuid(), (SELECT id FROM mail_mailboxes), 'ref-' || g, '',"
                " '', '', 0, timestamptz '2026-01-01' + g * interval '1 s'"
                " FROM generate_series(1, 10002) AS g"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO triage_results (id, message_id, category_id, priority, source)"
                " SELECT gen_random_uuid(), id, NULL, 2, 'llm' FROM mail_messages"
                " WHERE remote_ref <> 'ref-1'"
            )
        )
    await engine.dispose()

    await asyncio.to_thread(command.upgrade, config, "243731d5138f")

    engine = create_async_engine(empty_database, poolclass=NullPool)
    async with engine.begin() as connection:
        wrong = await connection.scalar(
            text(
                "SELECT count(*) FROM triage_results AS r JOIN mail_messages AS m"
                " ON m.id = r.message_id"
                " WHERE (r.mailbox_id, r.sort_date) IS DISTINCT FROM (m.mailbox_id, m.sort_date)"
            )
        )
        assert wrong == 0
        inserted = await connection.scalar(
            text(
                "INSERT INTO triage_results (id, message_id, category_id, priority, source)"
                " SELECT gen_random_uuid(), id, NULL, 2, 'llm' FROM mail_messages"
                " WHERE remote_ref = 'ref-1' RETURNING sort_date"
            )
        )
        assert inserted is not None and inserted.second == 1
        await connection.execute(
            text(
                "UPDATE mail_messages SET received_at = timestamptz '2030-01-01'"
                " WHERE remote_ref = 'ref-2'"
            )
        )
        changed = await connection.scalar(
            text(
                "SELECT r.sort_date FROM triage_results AS r JOIN mail_messages AS m"
                " ON m.id = r.message_id WHERE m.remote_ref = 'ref-2'"
            )
        )
        assert changed is not None and changed.year == 2030
    await engine.dispose()

    await asyncio.to_thread(command.downgrade, config, "93abef19553f")


async def test_triage_in_inbox_is_backfilled_in_batches(empty_database: str) -> None:
    """``triage_results_in_inbox`` marks the existing results of inbox messages (more than
    one batch); the triggers set the flag on new results and follow links and roles
    afterwards; downgrade restores the previous index."""
    config = alembic_config(empty_database)
    await asyncio.to_thread(command.upgrade, config, "a6e4b681ab6a")
    engine = create_async_engine(empty_database, poolclass=NullPool)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO users (id, email, display_name, role, language, timezone)"
                " VALUES (gen_random_uuid(), 'triage@example.org', 'Test', 'user', 'en', 'UTC')"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO mail_mailboxes (id, type, display_name, address, owner_user_id)"
                " SELECT gen_random_uuid(), 'imap', 'Test', 'test@example.org', id FROM users"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO mail_folders (id, mailbox_id, remote_id, name, kind, role)"
                " SELECT gen_random_uuid(), id, f, f, 'folder', r FROM mail_mailboxes,"
                " (VALUES ('INBOX', 'inbox'), ('Archive', 'archive')) AS v (f, r)"
            )
        )
        # 10,002 messages (two batches), every third in the inbox, all triaged.
        await connection.execute(
            text(
                "INSERT INTO mail_messages (id, mailbox_id, remote_ref, subject, body_text,"
                " body_main, size, received_at)"
                " SELECT gen_random_uuid(), (SELECT id FROM mail_mailboxes), 'ref-' || g, '',"
                " '', '', 0, timestamptz '2026-01-01' + g * interval '1 s'"
                " FROM generate_series(1, 10002) AS g"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO mail_message_folders (message_id, folder_id)"
                " SELECT m.id, f.id FROM mail_messages AS m JOIN mail_folders AS f"
                " ON f.role = CASE WHEN substr(m.remote_ref, 5)::int % 3 = 0"
                " THEN 'inbox' ELSE 'archive' END"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO triage_results (id, message_id, category_id, priority, source)"
                " SELECT gen_random_uuid(), id, NULL, 2, 'llm' FROM mail_messages"
            )
        )
    await engine.dispose()

    await asyncio.to_thread(command.upgrade, config, "b0213ba8e62e")

    engine = create_async_engine(empty_database, poolclass=NullPool)
    async with engine.begin() as connection:
        marked = await connection.execute(
            text(
                "SELECT r.in_inbox, count(*) FROM triage_results AS r"
                " JOIN mail_messages AS m ON m.id = r.message_id"
                " GROUP BY r.in_inbox, substr(m.remote_ref, 5)::int % 3 = 0"
                " HAVING r.in_inbox IS DISTINCT FROM (substr(m.remote_ref, 5)::int % 3 = 0)"
            )
        )
        assert marked.all() == []
        assert await connection.scalar(text("SELECT count(*) FROM triage_results WHERE in_inbox"))
        # Into the inbox: a new link, then the archive becomes an inbox; and out again.
        await connection.execute(
            text(
                "INSERT INTO mail_message_folders (message_id, folder_id)"
                " SELECT m.id, f.id FROM mail_messages AS m, mail_folders AS f"
                " WHERE m.remote_ref = 'ref-1' AND f.role = 'inbox'"
            )
        )
        await connection.execute(
            text(
                "DELETE FROM mail_message_folders AS l USING mail_messages AS m"
                " WHERE m.id = l.message_id AND m.remote_ref = 'ref-3'"
            )
        )
        flags = await connection.execute(
            text(
                "SELECT m.remote_ref, r.in_inbox FROM triage_results AS r"
                " JOIN mail_messages AS m ON m.id = r.message_id"
                " WHERE m.remote_ref IN ('ref-1', 'ref-2', 'ref-3') ORDER BY 1"
            )
        )
        assert flags.all() == [("ref-1", True), ("ref-2", False), ("ref-3", False)]
        await connection.execute(
            text("UPDATE mail_folders SET role = 'inbox' WHERE name = 'Archive'")
        )
        archived = await connection.scalar(
            text("SELECT count(*) FROM triage_results WHERE NOT in_inbox")
        )
        assert archived == 1  # ref-3, in no folder at all
    await engine.dispose()

    await asyncio.to_thread(command.downgrade, config, "a6e4b681ab6a")
    engine = create_async_engine(empty_database, poolclass=NullPool)
    async with engine.connect() as connection:
        indexes = await connection.execute(
            text("SELECT indexname FROM pg_indexes WHERE tablename = 'triage_results'")
        )
        names = {row[0] for row in indexes}
    await engine.dispose()
    assert "ix_triage_results_segment" in names
    assert "ix_triage_results_inbox_segment" not in names


async def _embedding_column(url: str) -> tuple[str, str, list[float]]:
    """Column type, HNSW operator class and the stored vector (single row)."""
    engine = create_async_engine(url, poolclass=NullPool)
    async with engine.connect() as connection:
        column_type = await connection.scalar(
            text(
                "SELECT format_type(atttypid, atttypmod) FROM pg_attribute"
                " WHERE attrelid = 'search_embeddings'::regclass AND attname = 'embedding'"
            )
        )
        opclass = await connection.scalar(
            text(
                "SELECT opc.opcname FROM pg_index i"
                " JOIN pg_class c ON c.oid = i.indexrelid"
                " JOIN pg_opclass opc ON opc.oid = i.indclass[0]"
                " WHERE c.relname = 'ix_search_embeddings_embedding_hnsw'"
            )
        )
        stored = await connection.scalar(text("SELECT embedding::text FROM search_embeddings"))
    await engine.dispose()
    return str(column_type), str(opclass), [float(v) for v in str(stored).strip("[]").split(",")]


async def test_embeddings_are_converted_to_halfvec_and_back(empty_database: str) -> None:
    """``store_embeddings_as_halfvec`` keeps existing vectors (16-bit precision), keeps
    the column's dimension and rebuilds the HNSW index for the new type; downgrade
    converts back."""
    config = alembic_config(empty_database)
    await asyncio.to_thread(command.upgrade, config, "5c672b257a5b")
    engine = create_async_engine(empty_database, poolclass=NullPool)
    async with engine.begin() as connection:
        # A column resized away from the configured default (``search resize``).
        await connection.execute(text("DROP INDEX ix_search_embeddings_embedding_hnsw"))
        await connection.execute(
            text("ALTER TABLE search_embeddings ALTER COLUMN embedding TYPE vector(3)")
        )
        await connection.execute(
            text(
                "CREATE INDEX ix_search_embeddings_embedding_hnsw ON search_embeddings"
                " USING hnsw (embedding vector_cosine_ops)"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO users (id, email, display_name, role, language, timezone)"
                " VALUES (gen_random_uuid(), 'vec@example.org', 'Test', 'user', 'en', 'UTC')"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO mail_mailboxes (id, type, display_name, address, owner_user_id)"
                " SELECT gen_random_uuid(), 'imap', 'Test', 'test@example.org', id FROM users"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO mail_messages (id, mailbox_id, remote_ref, subject, body_text,"
                " body_main, size) SELECT gen_random_uuid(), id, 'ref', '', '', '', 0"
                " FROM mail_mailboxes"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO search_chunks (id, message_id, mailbox_id, source, ordinal,"
                " heading, content, ts_config) SELECT gen_random_uuid(), id, mailbox_id,"
                " 'body', 0, '', 'text', 'simple' FROM mail_messages"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO search_embeddings (id, chunk_id, model, embedding)"
                " SELECT gen_random_uuid(), id, 'model', '[0.123456789, -0.5, 0.75]'"
                " FROM search_chunks"
            )
        )
    await engine.dispose()

    await asyncio.to_thread(command.upgrade, config, "5dab8560b40a")
    column_type, opclass, stored = await _embedding_column(empty_database)
    assert (column_type, opclass) == ("halfvec(3)", "halfvec_cosine_ops")
    assert stored == pytest.approx([0.123456789, -0.5, 0.75], abs=1e-3)

    await asyncio.to_thread(command.downgrade, config, "5c672b257a5b")
    column_type, opclass, stored = await _embedding_column(empty_database)
    assert (column_type, opclass) == ("vector(3)", "vector_cosine_ops")
    assert stored == pytest.approx([0.123456789, -0.5, 0.75], abs=1e-3)
