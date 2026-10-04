"""processing scan cursor

Keyset cursor for the check of all messages by ``processing.requeue_outdated`` (#226):
after a version bump each run continues where the previous one stopped instead of
reading again from the newest message. Nullable, no content; ``NULL`` starts from the
newest message as before.

Revision ID: 368ae36904f1
Revises: 450fb969fc30
Create Date: 2026-10-04 19:54:55.097477+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "368ae36904f1"
down_revision: str | Sequence[str] | None = "450fb969fc30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("processing_scan_state", sa.Column("cursor", sa.Uuid(), nullable=True))


def downgrade() -> None:
    op.drop_column("processing_scan_state", "cursor")
