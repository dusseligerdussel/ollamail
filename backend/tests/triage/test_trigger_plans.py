"""Plans of the lookups in the triage triggers when the statistics say "empty" (#242).

A ``VACUUM`` while a large transaction is open counts none of its rows: ``reltuples = 0``
for tables with many pages. The planner then expects at most one row from every scan, and
all indexes that hold a column cost the same; a lookup by key could scan a whole index for
every inserted row. The triggers are written so that only the primary key (or the unique
index) can serve each lookup; this test reproduces the statistics and checks the plans.

The statements below are the lookups of the trigger functions, with the PL/pgSQL variable
in place of ``$1``; the test checks that the functions still contain them verbatim.
"""

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = pytest.mark.db

MESSAGES = 5000
TABLES = ("mail_messages", "mail_message_folders", "triage_results", "mail_folders")
FUNCTIONS = (
    "triage_in_inbox",
    "triage_results_message_columns",
    "mail_message_folders_triage_inbox",
    "mail_folders_triage_inbox",
)


@dataclass(frozen=True, slots=True)
class Lookup:
    function: str
    # The statement as it is in the function (a ``SELECT`` from ``FROM`` on, because the
    # function selects ``INTO`` variables or uses ``PERFORM``).
    statement: str
    variable: str
    argument: str  # "message" or "folder"
    uses: tuple[str, ...]
    never: tuple[str, ...]


LOOKUPS = (
    Lookup(
        "triage_in_inbox",
        "FROM mail_folders WHERE id = ANY (ARRAY( SELECT folder_id FROM mail_message_folders"
        " WHERE message_id = message )) AND role = 'inbox'",
        "message",
        "message",
        uses=("pk_mail_message_folders",),
        never=("ix_mail_message_folders_folder_id", "Seq Scan on mail_message_folders"),
    ),
    Lookup(
        "triage_results_message_columns",
        "FROM mail_messages WHERE id = ANY (ARRAY[NEW.message_id]) ORDER BY id FOR SHARE",
        "NEW.message_id",
        "message",
        uses=("pk_mail_messages",),
        never=("ix_mail_messages_mailbox_id_sort_date_id", "Seq Scan on mail_messages", "Sort"),
    ),
    Lookup(
        "triage_results_message_columns",
        "FROM mail_folders WHERE id = ANY (ARRAY( SELECT folder_id FROM mail_message_folders"
        " WHERE message_id = NEW.message_id )) FOR KEY SHARE",
        "NEW.message_id",
        "message",
        uses=("pk_mail_message_folders",),
        never=("ix_mail_message_folders_folder_id", "Seq Scan on mail_message_folders"),
    ),
    Lookup(
        "mail_message_folders_triage_inbox",
        "FROM mail_messages WHERE id = ANY (ARRAY[link.message_id]) ORDER BY id FOR NO KEY UPDATE",
        "link.message_id",
        "message",
        uses=("pk_mail_messages",),
        never=("ix_mail_messages_mailbox_id_sort_date_id", "Seq Scan on mail_messages", "Sort"),
    ),
    Lookup(
        "mail_message_folders_triage_inbox",
        "UPDATE triage_results SET in_inbox = triage_in_inbox(message_id)"
        " WHERE message_id = link.message_id"
        " AND in_inbox IS DISTINCT FROM triage_in_inbox(message_id)",
        "link.message_id",
        "message",
        uses=("uq_triage_results_message_id",),
        never=("ix_triage_results_inbox_segment", "Seq Scan on triage_results"),
    ),
    Lookup(
        "mail_folders_triage_inbox",
        "UPDATE triage_results SET in_inbox = triage_in_inbox(message_id)"
        " WHERE message_id = ANY (ARRAY( SELECT message_id FROM mail_message_folders"
        " WHERE folder_id = ANY (ARRAY[NEW.id]) ORDER BY folder_id ))"
        " AND in_inbox IS DISTINCT FROM triage_in_inbox(message_id)",
        "NEW.id",
        "folder",
        uses=("ix_mail_message_folders_folder_id", "uq_triage_results_message_id"),
        never=(
            "pk_mail_message_folders",
            "Seq Scan on mail_message_folders",
            "Seq Scan on triage_results",
            "Sort",
        ),
    ),
)

_SEED = """
WITH owner AS (
    INSERT INTO users (id, email, display_name, role, language, timezone)
    VALUES (gen_random_uuid(), 'plans-' || gen_random_uuid() || '@example.org', 'Plans',
            'user', 'en', 'UTC')
    RETURNING id
)
INSERT INTO mail_mailboxes (id, owner_user_id, type, display_name, address)
SELECT gen_random_uuid(), id, 'imap', 'Plans', 'plans@example.org' FROM owner
RETURNING id
"""


def _normalised(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


def _explainable(lookup: Lookup) -> str:
    variable = rf"(?<![\w.]){re.escape(lookup.variable)}(?!\w)"
    statement = re.sub(variable, "$1", lookup.statement)
    return statement if statement.startswith("UPDATE") else f"SELECT {statement}"


@pytest.fixture
async def empty_statistics(migrated_database: str) -> AsyncIterator[AsyncConnection]:
    """A transaction with many uncommitted messages, links and results after a ``VACUUM``
    that saw none of them (as an autovacuum during a large sync or the perf seed)."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    vacuum = create_async_engine(
        migrated_database, poolclass=NullPool, isolation_level="AUTOCOMMIT"
    )
    tables = ", ".join(TABLES)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            mailbox = await connection.scalar(text(_SEED))
            folders = {}
            for remote_id, role in (("INBOX", "inbox"), ("Archive", "archive")):
                folders[role] = await connection.scalar(
                    text(
                        "INSERT INTO mail_folders (id, mailbox_id, remote_id, name, kind, role)"
                        " VALUES (gen_random_uuid(), :mailbox, :remote_id, :remote_id,"
                        " 'folder', :role) RETURNING id"
                    ),
                    {"mailbox": mailbox, "remote_id": remote_id, "role": role},
                )
            await connection.execute(
                text(
                    "INSERT INTO mail_messages (id, mailbox_id, remote_ref, message_id_header,"
                    ' subject, sender, "to", headers, received_at, body_text, body_main,'
                    " size, flags, has_attachments)"
                    " SELECT gen_random_uuid(), :mailbox, 'ref-' || g, '<' || g || '@x.org>',"
                    " 'Subject', '{}'::jsonb, '[]'::jsonb, '[]'::jsonb,"
                    " timestamptz '2026-09-30 12:00+00' - g * interval '1 minute',"
                    " repeat(md5(g::text), 10), 'Body', 1, ARRAY['seen'], false"
                    " FROM generate_series(1, :n) AS g"
                ),
                {"mailbox": mailbox, "n": MESSAGES},
            )
            await connection.execute(
                text(
                    "INSERT INTO mail_message_folders (message_id, folder_id)"
                    " SELECT id, CASE WHEN substr(remote_ref, 5)::int % 20 = 0"
                    " THEN CAST(:archive AS uuid) ELSE CAST(:inbox AS uuid) END"
                    " FROM mail_messages WHERE mailbox_id = :mailbox"
                ),
                {"mailbox": mailbox, **folders},
            )
            await connection.execute(
                text(
                    "INSERT INTO triage_results (id, message_id, priority, source, reason)"
                    " SELECT gen_random_uuid(), id, 2, 'llm', 'Synthetic reason.'"
                    " FROM mail_messages WHERE mailbox_id = :mailbox"
                ),
                {"mailbox": mailbox},
            )
            async with vacuum.connect() as other:
                await other.execute(text(f"VACUUM (ANALYZE) {tables}"))
            yield connection
            await transaction.rollback()
    finally:
        # Statistics for the empty tables again, for the tests that follow.
        async with vacuum.connect() as other:
            await other.execute(text(f"VACUUM (ANALYZE) {tables}"))
        await vacuum.dispose()
        await engine.dispose()


async def test_trigger_lookups_use_the_primary_keys_when_statistics_say_empty(
    empty_statistics: AsyncConnection,
) -> None:
    connection = empty_statistics
    stats = await connection.execute(
        text(
            "SELECT relname, reltuples, relpages FROM pg_class"
            " WHERE relname IN ('mail_messages', 'mail_message_folders', 'triage_results')"
        )
    )
    for relname, reltuples, relpages in stats:
        assert (reltuples, relpages > 0) == (0, True), relname

    sources = dict(
        (
            await connection.execute(
                text("SELECT proname, prosrc FROM pg_proc WHERE proname = ANY(:names)"),
                {"names": list(FUNCTIONS)},
            )
        ).all()
    )
    arguments = {
        "message": await connection.scalar(text("SELECT message_id FROM triage_results LIMIT 1")),
        "folder": await connection.scalar(
            text("SELECT id FROM mail_folders WHERE role = 'inbox' LIMIT 1")
        ),
    }
    for index, lookup in enumerate(LOOKUPS):
        assert _normalised(lookup.statement) in _normalised(sources[lookup.function]), lookup
        await connection.execute(text(f"PREPARE lookup_{index}(uuid) AS {_explainable(lookup)}"))
        for mode in ("force_generic_plan", "force_custom_plan"):
            await connection.execute(text(f"SET LOCAL plan_cache_mode = {mode}"))
            # The argument is a literal, so ``EXPLAIN EXECUTE`` plans it like a call.
            plan = "\n".join(
                (
                    await connection.execute(
                        text(f"EXPLAIN EXECUTE lookup_{index}('{arguments[lookup.argument]}')")
                    )
                ).scalars()
            )
            for name in lookup.uses:
                assert name in plan, f"{lookup.function} ({mode}) does not use {name}:\n{plan}"
            for name in lookup.never:
                assert name not in plan, f"{lookup.function} ({mode}) uses {name}:\n{plan}"


async def test_trigger_functions_plan_every_call(migrated_database: str) -> None:
    """Plans cached while a table was (nearly) empty would scan it sequentially after it
    has grown; the functions plan each statement for the table as it is."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    async with engine.connect() as connection:
        configs = dict(
            (
                await connection.execute(
                    text("SELECT proname, proconfig FROM pg_proc WHERE proname = ANY(:names)"),
                    {"names": list(FUNCTIONS)},
                )
            ).all()
        )
    await engine.dispose()
    assert configs == {name: ["plan_cache_mode=force_custom_plan"] for name in FUNCTIONS}
