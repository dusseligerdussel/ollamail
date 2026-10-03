"""add act assignment permission

Assignments of shared mailboxes may grant ``act`` (#148): users then archive, move, trash
and flag its mails and change their read state. Only the CHECK constraint of the
VARCHAR enum changes. The downgrade turns ``act`` assignments back into ``read`` ones.

Revision ID: 1966be4845c1
Revises: 7150e49202cb
Create Date: 2026-10-03 07:25:33.624794+00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "1966be4845c1"
down_revision: str | Sequence[str] | None = "7150e49202cb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "mail_mailbox_assignments"
CONSTRAINT = "ck_mail_mailbox_assignments_assignment_permission"


def upgrade() -> None:
    op.drop_constraint(op.f(CONSTRAINT), TABLE, type_="check")
    op.create_check_constraint(op.f(CONSTRAINT), TABLE, "permission IN ('read', 'act')")


def downgrade() -> None:
    op.execute(f"UPDATE {TABLE} SET permission = 'read' WHERE permission = 'act'")
    op.drop_constraint(op.f(CONSTRAINT), TABLE, type_="check")
    op.create_check_constraint(op.f(CONSTRAINT), TABLE, "permission IN ('read')")
