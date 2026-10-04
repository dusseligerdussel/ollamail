"""identity link notices

Notices for users about sign-ins linked to their account by e-mail address (#208): only user
ID, provider key and time; deleted with the user (``ON DELETE CASCADE``).

Revision ID: ed3357fe44bc
Revises: b5c86da9e237
Create Date: 2026-10-04 17:43:20.540382+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ed3357fe44bc"
down_revision: str | Sequence[str] | None = "b5c86da9e237"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "auth_identity_link_notices",
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
            name=op.f("fk_auth_identity_link_notices_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_identity_link_notices")),
    )
    op.create_index(
        op.f("ix_auth_identity_link_notices_user_id"),
        "auth_identity_link_notices",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_auth_identity_link_notices_user_id"), table_name="auth_identity_link_notices"
    )
    op.drop_table("auth_identity_link_notices")
