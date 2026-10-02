"""add idp admin tables

Revision ID: d8dd6f4b5840
Revises: 068c341b2ca9
Create Date: 2026-10-02 05:20:08.407955+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d8dd6f4b5840"
down_revision: str | Sequence[str] | None = "068c341b2ca9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Role checks are explicit CheckConstraints (named by the naming convention).
    op.create_table(
        "auth_policy",
        sa.Column("singleton", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "local_login_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column("role_mapping_enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "default_role",
            sa.Enum(
                "admin",
                "user",
                name="user_role",
                native_enum=False,
                create_constraint=False,
                length=16,
            ),
            nullable=False,
        ),
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
        sa.CheckConstraint(
            "default_role IN ('admin', 'user')", name=op.f("ck_auth_policy_user_role")
        ),
        sa.CheckConstraint("singleton", name=op.f("ck_auth_policy_singleton")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_policy")),
        sa.UniqueConstraint("singleton", name=op.f("uq_auth_policy_singleton")),
    )
    op.create_table(
        "auth_role_mapping_rules",
        sa.Column("group", sa.String(length=255), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=True),
        sa.Column(
            "role",
            sa.Enum(
                "admin",
                "user",
                name="user_role",
                native_enum=False,
                create_constraint=False,
                length=16,
            ),
            nullable=False,
        ),
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
        sa.CheckConstraint(
            "role IN ('admin', 'user')", name=op.f("ck_auth_role_mapping_rules_user_role")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_role_mapping_rules")),
    )
    op.create_table(
        "auth_invitations",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
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
            name=op.f("fk_auth_invitations_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_invitations")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_auth_invitations_token_hash")),
        sa.UniqueConstraint("user_id", name=op.f("uq_auth_invitations_user_id")),
    )
    op.create_index(
        op.f("ix_auth_invitations_expires_at"), "auth_invitations", ["expires_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_auth_invitations_expires_at"), table_name="auth_invitations")
    op.drop_table("auth_invitations")
    op.drop_table("auth_role_mapping_rules")
    op.drop_table("auth_policy")
