"""add message sort date

``mail_messages.sort_date`` (received, else sent, else stored) with the index
``(mailbox_id, sort_date DESC, id DESC)`` for the inbox and triage lists (#140). A trigger
keeps the column up to date. Existing rows are filled in batches, each committed on its own,
so large mailboxes neither hold all row locks at once nor fill the WAL in one transaction.

Revision ID: 7150e49202cb
Revises: 1efbd562bd72
Create Date: 2026-10-02 23:16:48.959057+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7150e49202cb"
down_revision: str | Sequence[str] | None = "1efbd562bd72"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BACKFILL_BATCH_SIZE = 10_000

SORT_DATE_FUNCTION = """
CREATE FUNCTION mail_messages_sort_date() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.sort_date := coalesce(NEW.received_at, NEW.sent_at, NEW.created_at);
    RETURN NEW;
END
$$
"""


def _backfill() -> None:
    """Fill ``sort_date`` of the existing rows in batches along the primary key."""
    connection = op.get_bind()
    last = None
    while True:
        query = "SELECT id FROM mail_messages"
        if last is not None:
            query += " WHERE id > :last"
        query += " ORDER BY id LIMIT :size"
        ids = connection.execute(
            sa.text(query), {"last": last, "size": BACKFILL_BATCH_SIZE}
        ).scalars()
        batch = list(ids)
        if not batch:
            return
        connection.execute(
            sa.text(
                "UPDATE mail_messages"
                " SET sort_date = coalesce(received_at, sent_at, created_at)"
                " WHERE id BETWEEN :first AND :last AND sort_date IS NULL"
            ),
            {"first": batch[0], "last": batch[-1]},
        )
        last = batch[-1]


def upgrade() -> None:
    op.add_column(
        "mail_messages",
        sa.Column(
            "sort_date",
            sa.DateTime(timezone=True),
            server_default=sa.FetchedValue(),
            nullable=True,
        ),
    )
    op.execute(SORT_DATE_FUNCTION)
    # Rows written during the backfill get their value from the trigger.
    op.execute(
        "CREATE TRIGGER mail_messages_sort_date"
        " BEFORE INSERT OR UPDATE OF received_at, sent_at, created_at, sort_date"
        " ON mail_messages FOR EACH ROW EXECUTE FUNCTION mail_messages_sort_date()"
    )
    with op.get_context().autocommit_block():
        _backfill()
    op.alter_column("mail_messages", "sort_date", nullable=False)
    op.create_index(
        "ix_mail_messages_mailbox_id_sort_date_id",
        "mail_messages",
        ["mailbox_id", sa.literal_column("sort_date DESC"), sa.literal_column("id DESC")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_mail_messages_mailbox_id_sort_date_id", table_name="mail_messages")
    op.execute("DROP TRIGGER mail_messages_sort_date ON mail_messages")
    op.execute("DROP FUNCTION mail_messages_sort_date()")
    op.drop_column("mail_messages", "sort_date")
