"""add scim provisioning (#95)

Revision ID: 460671df7101
Revises: 2d3ddb05b120
Create Date: 2026-10-02 11:28:55.845980+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "460671df7101"
down_revision: str | Sequence[str] | None = "2d3ddb05b120"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scim_config",
        sa.Column("singleton", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "link_providers",
            postgresql.ARRAY(sa.String(length=64)),
            server_default="{}",
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
        sa.CheckConstraint("singleton", name=op.f("ck_scim_config_singleton")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scim_config")),
        sa.UniqueConstraint("singleton", name=op.f("uq_scim_config_singleton")),
    )
    op.create_table(
        "scim_groups",
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scim_groups")),
    )
    op.create_index(
        op.f("ix_scim_groups_display_name"), "scim_groups", ["display_name"], unique=False
    )
    op.create_index(
        op.f("ix_scim_groups_external_id"), "scim_groups", ["external_id"], unique=False
    )
    op.create_table(
        "scim_group_members",
        sa.Column("group_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["scim_groups.id"],
            name=op.f("fk_scim_group_members_group_id_scim_groups"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_scim_group_members_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("group_id", "user_id", name=op.f("pk_scim_group_members")),
    )
    op.create_index(
        op.f("ix_scim_group_members_user_id"), "scim_group_members", ["user_id"], unique=False
    )
    op.create_table(
        "scim_tokens",
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("hint", sa.String(length=16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scim_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_scim_tokens_token_hash")),
    )
    op.create_table(
        "scim_users",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("user_name", sa.String(length=320), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=True),
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
            ["user_id"], ["users.id"], name=op.f("fk_scim_users_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scim_users")),
        sa.UniqueConstraint("user_id", name=op.f("uq_scim_users_user_id")),
    )
    op.create_index(op.f("ix_scim_users_external_id"), "scim_users", ["external_id"], unique=False)
    op.create_index(
        "uq_scim_users_user_name_lower",
        "scim_users",
        [sa.literal_column("lower(user_name)")],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_scim_users_user_name_lower", table_name="scim_users")
    op.drop_index(op.f("ix_scim_users_external_id"), table_name="scim_users")
    op.drop_table("scim_users")
    op.drop_table("scim_tokens")
    op.drop_index(op.f("ix_scim_group_members_user_id"), table_name="scim_group_members")
    op.drop_table("scim_group_members")
    op.drop_index(op.f("ix_scim_groups_external_id"), table_name="scim_groups")
    op.drop_index(op.f("ix_scim_groups_display_name"), table_name="scim_groups")
    op.drop_table("scim_groups")
    op.drop_table("scim_config")
