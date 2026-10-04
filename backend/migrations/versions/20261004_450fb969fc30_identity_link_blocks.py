"""identity link blocks

Providers a user unlinked from their account (#216): they may not link to it by e-mail
address again until the user lifts the block. Only user ID, provider key and time; deleted
with the user (``ON DELETE CASCADE``).

Revision ID: 450fb969fc30
Revises: ed3357fe44bc
Create Date: 2026-10-04 19:44:25.898677+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "450fb969fc30"
down_revision: str | Sequence[str] | None = "ed3357fe44bc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "auth_identity_link_blocks",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_auth_identity_link_blocks_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_identity_link_blocks")),
        sa.UniqueConstraint(
            "user_id", "provider", name=op.f("uq_auth_identity_link_blocks_user_id")
        ),
    )


def downgrade() -> None:
    op.drop_table("auth_identity_link_blocks")
