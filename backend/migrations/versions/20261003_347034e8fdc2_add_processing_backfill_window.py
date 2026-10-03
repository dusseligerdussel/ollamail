"""add processing backfill window

Mails older than the backfill window skip the LLM classification steps (#141): new step
status ``skipped`` and the per-mailbox opt-in ``include_older``.

Revision ID: 347034e8fdc2
Revises: 7150e49202cb
Create Date: 2026-10-03 07:10:38.732045+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "347034e8fdc2"
down_revision: str | Sequence[str] | None = "7150e49202cb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Autogenerate does not compare check constraints; the status check is replaced by hand.
_STATUS_CHECK = "step_status"


def upgrade() -> None:
    op.add_column(
        "processing_mailbox_settings",
        sa.Column("include_older", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.drop_constraint(op.f("ck_message_processing_step_status"), "message_processing")
    op.create_check_constraint(
        _STATUS_CHECK,
        "message_processing",
        "status IN ('pending', 'running', 'done', 'failed', 'skipped')",
    )


def downgrade() -> None:
    op.execute("UPDATE message_processing SET status = 'pending' WHERE status = 'skipped'")
    op.drop_constraint(op.f("ck_message_processing_step_status"), "message_processing")
    op.create_check_constraint(
        _STATUS_CHECK,
        "message_processing",
        "status IN ('pending', 'running', 'done', 'failed')",
    )
    op.drop_column("processing_mailbox_settings", "include_older")
