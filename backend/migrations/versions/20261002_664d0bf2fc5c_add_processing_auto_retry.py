"""add processing auto retry

Automatic retries of steps that failed for a passing reason (#138).

Revision ID: 664d0bf2fc5c
Revises: 1efbd562bd72
Create Date: 2026-10-02 23:05:04.218218+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "664d0bf2fc5c"
down_revision: str | Sequence[str] | None = "1efbd562bd72"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "message_processing", sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "message_processing",
        sa.Column("auto_retries", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_index(
        op.f("ix_message_processing_retry_at"), "message_processing", ["retry_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_message_processing_retry_at"), table_name="message_processing")
    op.drop_column("message_processing", "auto_retries")
    op.drop_column("message_processing", "retry_at")
