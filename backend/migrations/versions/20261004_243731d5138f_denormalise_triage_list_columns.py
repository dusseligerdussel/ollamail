"""denormalise triage list columns

Inbox and triage lists on large mailboxes (#186):

- ``triage_results.mailbox_id`` and ``triage_results.sort_date``, copies of the message's
  columns, with the index ``(mailbox_id, category_id, priority, sort_date DESC,
  message_id DESC)``: a segment (category x priority) of the triage inbox is read in list
  order from its own index range instead of walking the whole mailbox. The trigger
  ``triage_results_message_columns`` fills both on insert; ``mail_messages_triage_sort_date``
  follows a changed ``sort_date`` of the message.
- ``ix_mail_messages_unread``: the list index restricted to unread messages, for
  ``unread=true``.

Existing rows are filled in batches, each committed on its own, like ``add_message_sort_date``.

Revision ID: 243731d5138f
Revises: 93abef19553f
Create Date: 2026-10-04 12:41:52.533169+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "243731d5138f"
down_revision: str | Sequence[str] | None = "93abef19553f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BACKFILL_BATCH_SIZE = 10_000

# ``FOR SHARE`` waits for a concurrent change of the message's ``sort_date`` and reads the
# new value; a change after the lock sees this row in ``mail_messages_triage_sort_date``.
MESSAGE_COLUMNS_FUNCTION = """
CREATE FUNCTION triage_results_message_columns() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    SELECT mailbox_id, sort_date INTO NEW.mailbox_id, NEW.sort_date
    FROM mail_messages WHERE id = NEW.message_id FOR SHARE;
    RETURN NEW;
END
$$
"""

SORT_DATE_FUNCTION = """
CREATE FUNCTION mail_messages_triage_sort_date() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    UPDATE triage_results SET sort_date = NEW.sort_date WHERE message_id = NEW.id;
    RETURN NULL;
END
$$
"""


def _backfill() -> None:
    """Fill both columns of the existing rows in batches along the primary key."""
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
                "UPDATE triage_results AS r"
                " SET mailbox_id = m.mailbox_id, sort_date = m.sort_date"
                " FROM mail_messages AS m"
                " WHERE m.id = r.message_id AND r.id BETWEEN :first AND :last"
                " AND r.mailbox_id IS NULL"
            ),
            {"first": batch[0], "last": batch[-1]},
        )
        last = batch[-1]


def upgrade() -> None:
    op.add_column(
        "triage_results",
        sa.Column("mailbox_id", sa.Uuid(), server_default=sa.FetchedValue(), nullable=True),
    )
    op.add_column(
        "triage_results",
        sa.Column(
            "sort_date",
            sa.DateTime(timezone=True),
            server_default=sa.FetchedValue(),
            nullable=True,
        ),
    )
    op.execute(MESSAGE_COLUMNS_FUNCTION)
    op.execute(SORT_DATE_FUNCTION)
    # Rows written during the backfill get their values from the triggers.
    op.execute(
        "CREATE TRIGGER triage_results_message_columns"
        " BEFORE INSERT OR UPDATE OF message_id, mailbox_id, sort_date ON triage_results"
        " FOR EACH ROW EXECUTE FUNCTION triage_results_message_columns()"
    )
    # Not ``UPDATE OF sort_date``: the column is set by the BEFORE trigger
    # ``mail_messages_sort_date``, which a column list would not see.
    op.execute(
        "CREATE TRIGGER mail_messages_triage_sort_date"
        " AFTER UPDATE ON mail_messages FOR EACH ROW"
        " WHEN (OLD.sort_date IS DISTINCT FROM NEW.sort_date)"
        " EXECUTE FUNCTION mail_messages_triage_sort_date()"
    )
    with op.get_context().autocommit_block():
        _backfill()
    op.alter_column("triage_results", "mailbox_id", nullable=False)
    op.alter_column("triage_results", "sort_date", nullable=False)
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
    op.create_index(
        "ix_mail_messages_unread",
        "mail_messages",
        ["mailbox_id", sa.literal_column("sort_date DESC"), sa.literal_column("id DESC")],
        unique=False,
        postgresql_where=sa.text("NOT (flags @> ARRAY['seen']::text[])"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_mail_messages_unread",
        table_name="mail_messages",
        postgresql_where=sa.text("NOT (flags @> ARRAY['seen']::text[])"),
    )
    op.drop_index("ix_triage_results_segment", table_name="triage_results")
    op.execute("DROP TRIGGER mail_messages_triage_sort_date ON mail_messages")
    op.execute("DROP TRIGGER triage_results_message_columns ON triage_results")
    op.execute("DROP FUNCTION mail_messages_triage_sort_date()")
    op.execute("DROP FUNCTION triage_results_message_columns()")
    op.drop_column("triage_results", "sort_date")
    op.drop_column("triage_results", "mailbox_id")
