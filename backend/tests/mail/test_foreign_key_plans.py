"""Plans of the foreign key checks when the statistics say "empty" (#246).

PostgreSQL checks a foreign key once per inserted or updated row with
``SELECT 1 FROM ONLY <table> x WHERE <key> = $1 FOR KEY SHARE OF x``. A ``VACUUM`` while a
large transaction is open counts none of its rows: ``reltuples = 0`` for tables with many
pages. The planner then expects at most one row from every scan, all indexes with the key
as a key column cost the same, and on a tie it keeps the most recently created one. The
list index of ``mail_messages`` had ``id`` as its last key column and was read in full for
every checked row. So no index but the one of the constraint may serve a lookup by a
referenced column: the list index is partial (``WHERE mailbox_id IS NOT NULL``), which a
lookup by ``id`` does not imply.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = pytest.mark.db

MESSAGES = 5000
# The tables a large sync writes, with the foreign keys pointing at them.
TABLES = ("mail_mailboxes", "mail_folders", "mail_messages")
# Tables large enough that a sequential scan per checked row would hurt.
LARGE = "mail_messages"

_SEED = """
WITH owner AS (
    INSERT INTO users (id, email, display_name, role, language, timezone)
    VALUES (gen_random_uuid(), 'fk-' || gen_random_uuid() || '@example.org', 'FK',
            'user', 'en', 'UTC')
    RETURNING id
)
INSERT INTO mail_mailboxes (id, owner_user_id, type, display_name, address)
SELECT gen_random_uuid(), id, 'imap', 'FK', 'fk@example.org' FROM owner
RETURNING id
"""

# Foreign keys to ``TABLES``: constraint, referenced table and column, index of the key.
_FOREIGN_KEYS = """
SELECT c.conname, c.confrelid::regclass::text, a.attname, c.conindid::regclass::text
FROM pg_constraint c
JOIN pg_attribute a ON a.attrelid = c.confrelid AND a.attnum = c.confkey[1]
WHERE c.contype = 'f' AND c.confrelid::regclass::text = ANY(:tables)
ORDER BY c.conname
"""

# Indexes other than the one of a foreign key that hold a referenced column as a key
# column (``INCLUDE`` columns are past ``indnkeyatts``), on any table. Partial indexes are
# left to the plans above: a lookup by key does not imply their predicates.
_COMPETING_INDEXES = """
SELECT DISTINCT c.conname, i.indexrelid::regclass::text
FROM pg_constraint c
JOIN pg_index i ON i.indrelid = c.confrelid AND i.indexrelid <> c.conindid
WHERE c.contype = 'f' AND i.indpred IS NULL
  AND c.confkey && (i.indkey::int2[])[0:i.indnkeyatts - 1]
ORDER BY 1, 2
"""


@pytest.fixture
async def empty_statistics(migrated_database: str) -> AsyncIterator[AsyncConnection]:
    """A transaction with many uncommitted messages after a ``VACUUM`` that saw none of
    them, as an autovacuum during a large sync or the perf seed."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    vacuum = create_async_engine(
        migrated_database, poolclass=NullPool, isolation_level="AUTOCOMMIT"
    )
    # ``PARALLEL 0``: a parallel index cleanup keeps its dead rows in shared memory, more
    # than the 64 MB ``/dev/shm`` of a container.
    statement = text(f"VACUUM (ANALYZE, INDEX_CLEANUP ON, PARALLEL 0) {', '.join(TABLES)}")
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            mailbox = await connection.scalar(text(_SEED))
            await connection.execute(
                text(
                    "INSERT INTO mail_folders (id, mailbox_id, remote_id, name, kind, role)"
                    " VALUES (gen_random_uuid(), :mailbox, 'INBOX', 'INBOX', 'folder', 'inbox')"
                ),
                {"mailbox": mailbox},
            )
            await connection.execute(
                text(
                    "INSERT INTO mail_messages (id, mailbox_id, remote_ref, subject, received_at,"
                    " body_text, body_main, size, flags, has_attachments)"
                    " SELECT gen_random_uuid(), :mailbox, 'ref-' || g, 'Subject',"
                    " timestamptz '2026-09-30 12:00+00' - g * interval '1 minute',"
                    " repeat(md5(g::text), 10), 'Body', 1, ARRAY['seen'], false"
                    " FROM generate_series(1, :n) AS g"
                ),
                {"mailbox": mailbox, "n": MESSAGES},
            )
            async with vacuum.connect() as other:
                await other.execute(statement)
            yield connection
            await transaction.rollback()
    finally:
        # Statistics for the empty tables again, for the tests that follow.
        async with vacuum.connect() as other:
            await other.execute(statement)
        await vacuum.dispose()
        await engine.dispose()


async def test_foreign_key_checks_use_their_index_when_statistics_say_empty(
    empty_statistics: AsyncConnection,
) -> None:
    connection = empty_statistics
    reltuples, relpages = (
        await connection.execute(
            text("SELECT reltuples, relpages FROM pg_class WHERE relname = :name"),
            {"name": LARGE},
        )
    ).one()
    assert reltuples == 0
    assert relpages > 0

    foreign_keys = (await connection.execute(text(_FOREIGN_KEYS), {"tables": list(TABLES)})).all()
    assert {table for _, table, _, _ in foreign_keys} == set(TABLES)
    for number, (constraint, table, column, key_index) in enumerate(foreign_keys):
        indexes = set(
            await connection.scalars(
                text(
                    "SELECT indexrelid::regclass::text FROM pg_index"
                    " WHERE indrelid = CAST(:t AS regclass)"
                ),
                {"t": table},
            )
        )
        argument = await connection.scalar(text(f"SELECT {column} FROM {table} LIMIT 1"))
        # The statement of ``RI_FKey_check`` (ri_triggers.c), without the quoting.
        await connection.execute(
            text(
                f"PREPARE fk_{number}(uuid) AS SELECT 1 FROM ONLY {table} x"
                f" WHERE {column} = $1 FOR KEY SHARE OF x"
            )
        )
        for mode in ("force_generic_plan", "force_custom_plan"):
            await connection.execute(text(f"SET LOCAL plan_cache_mode = {mode}"))
            plan = "\n".join(
                (await connection.execute(text(f"EXPLAIN EXECUTE fk_{number}('{argument}')")))
                .scalars()
                .all()
            )
            where = f"{constraint} ({mode})"
            for index in indexes - {key_index}:
                assert f" {index} " not in plan, f"{where} uses {index}:\n{plan}"
            if table == LARGE:
                assert f" {key_index} " in plan, f"{where} does not use {key_index}:\n{plan}"


async def test_no_index_competes_with_a_foreign_key(migrated_database: str) -> None:
    """Any table: a referenced column is a key column only of the index of the key."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    async with engine.connect() as connection:
        competing = (await connection.execute(text(_COMPETING_INDEXES))).all()
    await engine.dispose()
    assert competing == []
