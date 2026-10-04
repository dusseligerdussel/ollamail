"""triage results in inbox

Triage inbox on mailboxes with a large archive (#225): ``triage_results.in_inbox`` says
whether the message is in a folder with the role ``inbox``, and the segment index only holds
these results (``ix_triage_results_inbox_segment``, replaces ``ix_triage_results_segment``).
Counts, segments and the untriaged messages of the triage inbox then read the inbox, not all
results of the mailbox.

The flag is kept up to date by triggers, like ``mailbox_id`` and ``sort_date``
(``denormalise_triage_list_columns``):

- ``triage_results_message_columns`` sets it on insert (and whenever it is written).
- ``mail_message_folders_triage_inbox`` follows a message into or out of an inbox folder.
- ``mail_folders_triage_inbox`` follows a folder that gets or loses the role ``inbox``.

Concurrent changes meet on a row lock, so neither side misses the other: the message row
(``FOR SHARE`` on insert of a result, ``FOR NO KEY UPDATE`` on a change of its folders) and
the folder row (``FOR KEY SHARE`` on insert of a result and on a change of its messages,
``FOR UPDATE`` on a change of its role). ``FOR KEY SHARE`` is what the foreign key of a new
link takes anyway, so other updates of a folder (``sync_enabled``) do not wait for a sync.

Existing rows are filled in batches, each committed on its own, like
``denormalise_triage_list_columns``.

Revision ID: b0213ba8e62e
Revises: ed3357fe44bc
Create Date: 2026-10-04 20:03:46.568545+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b0213ba8e62e"
down_revision: str | Sequence[str] | None = "ed3357fe44bc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BACKFILL_BATCH_SIZE = 10_000

# PL/pgSQL, not SQL: the triggers call it once per row, and only PL/pgSQL keeps the plan
# (a SQL function that cannot be inlined is planned on every call).
IN_INBOX_FUNCTION = """
CREATE FUNCTION triage_in_inbox(message uuid) RETURNS boolean LANGUAGE plpgsql STABLE AS $$
BEGIN
    RETURN EXISTS (
        SELECT FROM mail_message_folders AS mf JOIN mail_folders AS f ON f.id = mf.folder_id
        WHERE mf.message_id = message AND f.role = 'inbox'
    );
END
$$
"""

# ``FOR SHARE`` on the message waits for a concurrent change of its ``sort_date`` or its
# folders and reads the new state; a change after the lock sees this row in
# ``mail_messages_triage_sort_date`` or ``mail_message_folders_triage_inbox``. The lock on
# the folders does the same for a change of their role (``mail_folders_triage_inbox``).
# ``in_inbox`` is read by a statement of its own, after the locks, so it sees what they
# waited for.
MESSAGE_COLUMNS_FUNCTION = """
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

PREVIOUS_MESSAGE_COLUMNS_FUNCTION = """
CREATE OR REPLACE FUNCTION triage_results_message_columns() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    SELECT mailbox_id, sort_date INTO NEW.mailbox_id, NEW.sort_date
    FROM mail_messages WHERE id = NEW.message_id FOR SHARE;
    RETURN NEW;
END
$$
"""

# Only a link to an inbox folder can change ``in_inbox``; a folder that is gone (deleted
# with its links) may have been one. The lock on the message waits for a result being
# inserted (``triage_results_message_columns``), so the update below sees it.
MESSAGE_FOLDERS_FUNCTION = """
CREATE FUNCTION mail_message_folders_triage_inbox() RETURNS trigger LANGUAGE plpgsql AS $$
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

# ``FOR UPDATE`` waits for the transactions that link messages to the folder or insert
# results of its messages (``FOR KEY SHARE``), so the update below sees their rows.
FOLDERS_FUNCTION = """
CREATE FUNCTION mail_folders_triage_inbox() RETURNS trigger LANGUAGE plpgsql AS $$
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


def _message_columns_trigger(columns: str) -> None:
    op.execute("DROP TRIGGER triage_results_message_columns ON triage_results")
    op.execute(
        "CREATE TRIGGER triage_results_message_columns"
        f" BEFORE INSERT OR UPDATE OF {columns} ON triage_results"
        " FOR EACH ROW EXECUTE FUNCTION triage_results_message_columns()"
    )


def _backfill() -> None:
    """Set ``in_inbox`` of the existing rows in batches along the primary key (the trigger
    computes the value)."""
    connection = op.get_bind()
    last = None
    while True:
        query = "SELECT id FROM triage_results"
        if last is not None:
            query += " WHERE id > :last"
        query += " ORDER BY id LIMIT :size"
        ids = connection.execute(sa.text(query), {"last": last, "size": BACKFILL_BATCH_SIZE})
        batch = list(ids.scalars())
        if not batch:
            return
        connection.execute(
            sa.text(
                "UPDATE triage_results SET in_inbox = true"
                " WHERE id BETWEEN :first AND :last AND NOT in_inbox"
                " AND triage_in_inbox(message_id)"
            ),
            {"first": batch[0], "last": batch[-1]},
        )
        last = batch[-1]


def upgrade() -> None:
    op.add_column(
        "triage_results",
        sa.Column("in_inbox", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.execute(IN_INBOX_FUNCTION)
    op.execute(MESSAGE_COLUMNS_FUNCTION)
    op.execute(MESSAGE_FOLDERS_FUNCTION)
    op.execute(FOLDERS_FUNCTION)
    # Rows written during the backfill get their value from the triggers.
    _message_columns_trigger("message_id, mailbox_id, sort_date, in_inbox")
    op.execute(
        "CREATE TRIGGER mail_message_folders_triage_inbox"
        " AFTER INSERT OR DELETE ON mail_message_folders"
        " FOR EACH ROW EXECUTE FUNCTION mail_message_folders_triage_inbox()"
    )
    op.execute(
        "CREATE TRIGGER mail_folders_triage_inbox AFTER UPDATE OF role ON mail_folders"
        " FOR EACH ROW WHEN (OLD.role IS DISTINCT FROM NEW.role)"
        " EXECUTE FUNCTION mail_folders_triage_inbox()"
    )
    with op.get_context().autocommit_block():
        _backfill()
    op.drop_index("ix_triage_results_segment", table_name="triage_results")
    op.create_index(
        "ix_triage_results_inbox_segment",
        "triage_results",
        [
            "mailbox_id",
            "category_id",
            "priority",
            sa.literal_column("sort_date DESC"),
            sa.literal_column("message_id DESC"),
        ],
        unique=False,
        postgresql_where=sa.text("in_inbox"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_triage_results_inbox_segment",
        table_name="triage_results",
        postgresql_where=sa.text("in_inbox"),
    )
    op.create_index(
        "ix_triage_results_segment",
        "triage_results",
        [
            "mailbox_id",
            "category_id",
            "priority",
            sa.literal_column("sort_date DESC"),
            sa.literal_column("message_id DESC"),
        ],
        unique=False,
    )
    op.execute("DROP TRIGGER mail_folders_triage_inbox ON mail_folders")
    op.execute("DROP TRIGGER mail_message_folders_triage_inbox ON mail_message_folders")
    _message_columns_trigger("message_id, mailbox_id, sort_date")
    op.execute(PREVIOUS_MESSAGE_COLUMNS_FUNCTION)
    op.execute("DROP FUNCTION mail_folders_triage_inbox()")
    op.execute("DROP FUNCTION mail_message_folders_triage_inbox()")
    op.execute("DROP FUNCTION triage_in_inbox(uuid)")
    op.drop_column("triage_results", "in_inbox")
