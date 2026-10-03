"""add user deletion requested

Users with mailboxes are deleted in the background (#177): the mailboxes in batches, then
the user row. The column marks a requested deletion.

Revision ID: d84e1e94a236
Revises: 5c672b257a5b
Create Date: 2026-10-03 22:52:50.974289+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d84e1e94a236"
down_revision: str | Sequence[str] | None = "5c672b257a5b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users", sa.Column("deletion_requested_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("users", "deletion_requested_at")
