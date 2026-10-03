"""add mailbox deletion requested

Mailboxes are removed by a background job in batches (#147); the column marks a
requested removal.

Revision ID: 5c672b257a5b
Revises: 7150e49202cb
Create Date: 2026-10-03 07:27:04.058028+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5c672b257a5b"
down_revision: str | Sequence[str] | None = "7150e49202cb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "mail_mailboxes",
        sa.Column("deletion_requested_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("mail_mailboxes", "deletion_requested_at")
