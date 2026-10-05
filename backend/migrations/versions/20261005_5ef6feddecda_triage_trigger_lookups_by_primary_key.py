"""triage trigger lookups by primary key

The triggers that keep ``triage_results.mailbox_id``, ``sort_date`` and ``in_inbox`` up to
date (``denormalise_triage_list_columns``, ``triage_results_in_inbox``) look up a message
and its folders once per row. Two things made these lookups scan whole indexes or tables,
so that inserting many rows took quadratic time (#242):

- **Statistics that say "empty".** A ``VACUUM`` while a large transaction is still open
  counts none of its rows and writes ``reltuples = 0`` for tables that already have many
  pages. The planner then expects every scan to return at most one row, and all indexes
  that hold the column cost the same. On a tie it keeps the most recently created index:
  ``ix_mail_messages_mailbox_id_sort_date_id`` for ``mail_messages.id`` (full index scan,
  ``id`` is its last column) and, in the joins, ``ix_mail_message_folders_folder_id``.
- **Plans cached for another table size.** PL/pgSQL keeps its plans for the whole
  connection. A plan made while a table was (nearly) empty scans it sequentially and is
  kept while the table grows, until the next ``ANALYZE``.

The lookups are therefore written so that only the primary key (or the unique index) can
serve them without a sequential scan or a sort, and the functions run with
``enable_seqscan = off`` and ``enable_sort = off``. Each lookup then has one plan, the same
for any statistics and any table size, so the cached plans stay right. (Planning every call
instead, ``plan_cache_mode = force_custom_plan``, cost three times as much on a normal
insert.)

- No joins: the folders of a message are read from ``pk_mail_message_folders`` first, then
  ``mail_folders`` by primary key (``id = ANY (ARRAY(...))``).
- A message is looked up as ``id = ANY (ARRAY[...]) ORDER BY id``: only the primary key
  returns rows in this order; the composite index would need a sort. (With ``id = ...`` the
  order would be redundant, and on a tie the planner may still pick the composite index,
  e.g. when the primary key has more levels.) The links of a folder likewise use
  ``ix_mail_message_folders_folder_id``, not ``pk_mail_message_folders``.

Locks, their order and the rows they cover are unchanged.

Revision ID: 5ef6feddecda
Revises: b0213ba8e62e
Create Date: 2026-10-05 12:27:12.808388+00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "5ef6feddecda"
down_revision: str | Sequence[str] | None = "b0213ba8e62e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PLAN = "SET enable_seqscan = off SET enable_sort = off"

IN_INBOX_FUNCTION = f"""
CREATE OR REPLACE FUNCTION triage_in_inbox(message uuid) RETURNS boolean
LANGUAGE plpgsql STABLE {PLAN} AS $$
BEGIN
    RETURN EXISTS (
        SELECT FROM mail_folders
        WHERE id = ANY (ARRAY(
                SELECT folder_id FROM mail_message_folders WHERE message_id = message
            ))
            AND role = 'inbox'
    );
END
$$
"""

MESSAGE_COLUMNS_FUNCTION = f"""
CREATE OR REPLACE FUNCTION triage_results_message_columns() RETURNS trigger
LANGUAGE plpgsql {PLAN} AS $$
BEGIN
    SELECT mailbox_id, sort_date INTO NEW.mailbox_id, NEW.sort_date
    FROM mail_messages WHERE id = ANY (ARRAY[NEW.message_id]) ORDER BY id FOR SHARE;
    PERFORM FROM mail_folders
    WHERE id = ANY (ARRAY(
            SELECT folder_id FROM mail_message_folders WHERE message_id = NEW.message_id
        ))
    FOR KEY SHARE;
    NEW.in_inbox := triage_in_inbox(NEW.message_id);
    RETURN NEW;
END
$$
"""

MESSAGE_FOLDERS_FUNCTION = f"""
CREATE OR REPLACE FUNCTION mail_message_folders_triage_inbox() RETURNS trigger
LANGUAGE plpgsql {PLAN} AS $$
DECLARE
    link record;
    folder_role text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        link := OLD;
    ELSE
        link := NEW;
    END IF;
    SELECT f.role INTO folder_role FROM mail_folders AS f
    WHERE f.id = link.folder_id FOR KEY SHARE;
    IF FOUND AND folder_role IS DISTINCT FROM 'inbox' THEN
        RETURN NULL;
    END IF;
    PERFORM FROM mail_messages
    WHERE id = ANY (ARRAY[link.message_id]) ORDER BY id FOR NO KEY UPDATE;
    UPDATE triage_results SET in_inbox = triage_in_inbox(message_id)
    WHERE message_id = link.message_id
        AND in_inbox IS DISTINCT FROM triage_in_inbox(message_id);
    RETURN NULL;
END
$$
"""

FOLDERS_FUNCTION = f"""
CREATE OR REPLACE FUNCTION mail_folders_triage_inbox() RETURNS trigger
LANGUAGE plpgsql {PLAN} AS $$
BEGIN
    PERFORM FROM mail_folders WHERE id = NEW.id FOR UPDATE;
    UPDATE triage_results SET in_inbox = triage_in_inbox(message_id)
    WHERE message_id = ANY (ARRAY(
            SELECT message_id FROM mail_message_folders
            WHERE folder_id = ANY (ARRAY[NEW.id]) ORDER BY folder_id
        ))
        AND in_inbox IS DISTINCT FROM triage_in_inbox(message_id);
    RETURN NULL;
END
$$
"""

# As created by ``b0213ba8e62e`` (``CREATE OR REPLACE`` also drops the ``SET`` option).
PREVIOUS_IN_INBOX_FUNCTION = """
CREATE OR REPLACE FUNCTION triage_in_inbox(message uuid) RETURNS boolean
LANGUAGE plpgsql STABLE AS $$
BEGIN
    RETURN EXISTS (
        SELECT FROM mail_message_folders AS mf JOIN mail_folders AS f ON f.id = mf.folder_id
        WHERE mf.message_id = message AND f.role = 'inbox'
    );
END
$$
"""

PREVIOUS_MESSAGE_COLUMNS_FUNCTION = """
CREATE OR REPLACE FUNCTION triage_results_message_columns() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    SELECT mailbox_id, sort_date INTO NEW.mailbox_id, NEW.sort_date
    FROM mail_messages WHERE id = NEW.message_id FOR SHARE;
    PERFORM FROM mail_folders AS f JOIN mail_message_folders AS mf ON mf.folder_id = f.id
    WHERE mf.message_id = NEW.message_id FOR KEY SHARE OF f;
    NEW.in_inbox := triage_in_inbox(NEW.message_id);
    RETURN NEW;
END
$$
"""

PREVIOUS_MESSAGE_FOLDERS_FUNCTION = """
CREATE OR REPLACE FUNCTION mail_message_folders_triage_inbox() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    link record;
    folder_role text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        link := OLD;
    ELSE
        link := NEW;
    END IF;
    SELECT f.role INTO folder_role FROM mail_folders AS f
    WHERE f.id = link.folder_id FOR KEY SHARE;
    IF FOUND AND folder_role IS DISTINCT FROM 'inbox' THEN
        RETURN NULL;
    END IF;
    PERFORM FROM mail_messages WHERE id = link.message_id FOR NO KEY UPDATE;
    UPDATE triage_results SET in_inbox = triage_in_inbox(message_id)
    WHERE message_id = link.message_id
        AND in_inbox IS DISTINCT FROM triage_in_inbox(message_id);
    RETURN NULL;
END
$$
"""

PREVIOUS_FOLDERS_FUNCTION = """
CREATE OR REPLACE FUNCTION mail_folders_triage_inbox() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM FROM mail_folders WHERE id = NEW.id FOR UPDATE;
    UPDATE triage_results AS r SET in_inbox = triage_in_inbox(r.message_id)
    FROM mail_message_folders AS mf
    WHERE mf.folder_id = NEW.id AND r.message_id = mf.message_id
        AND r.in_inbox IS DISTINCT FROM triage_in_inbox(r.message_id);
    RETURN NULL;
END
$$
"""


def upgrade() -> None:
    op.execute(IN_INBOX_FUNCTION)
    op.execute(MESSAGE_COLUMNS_FUNCTION)
    op.execute(MESSAGE_FOLDERS_FUNCTION)
    op.execute(FOLDERS_FUNCTION)


def downgrade() -> None:
    op.execute(PREVIOUS_IN_INBOX_FUNCTION)
    op.execute(PREVIOUS_MESSAGE_COLUMNS_FUNCTION)
    op.execute(PREVIOUS_MESSAGE_FOLDERS_FUNCTION)
    op.execute(PREVIOUS_FOLDERS_FUNCTION)
