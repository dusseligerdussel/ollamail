"""add digests

Revision ID: 4f174bab2c45
Revises: 068c341b2ca9
Create Date: 2026-10-02 05:28:53.317218+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "4f174bab2c45"
down_revision: str | Sequence[str] | None = "068c341b2ca9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The enum checks are the explicit CheckConstraints below (named by the naming convention).
    op.create_table(
        "digest_user_settings",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("delivery_time", sa.Time(), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=True),
        sa.Column("weekdays", postgresql.ARRAY(sa.SmallInteger()), nullable=False),
        sa.Column("language", sa.String(length=8), nullable=True),
        sa.Column("voice", sa.String(length=128), nullable=True),
        sa.Column(
            "length",
            sa.Enum(
                "short",
                "normal",
                name="digest_length",
                native_enum=False,
                create_constraint=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("mailbox_ids", postgresql.ARRAY(sa.Uuid()), nullable=True),
        sa.Column("last_scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("feed_token_hash", sa.String(length=64), nullable=True),
        sa.Column("feed_token_created_at", sa.DateTime(timezone=True), nullable=True),
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
            "length IN ('short', 'normal')", name=op.f("ck_digest_user_settings_digest_length")
        ),
        sa.CheckConstraint(
            "weekdays <@ ARRAY[0,1,2,3,4,5,6]::smallint[]",
            name=op.f("ck_digest_user_settings_weekdays"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_digest_user_settings_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_digest_user_settings")),
        sa.UniqueConstraint(
            "feed_token_hash", name=op.f("uq_digest_user_settings_feed_token_hash")
        ),
        sa.UniqueConstraint("user_id", name=op.f("uq_digest_user_settings_user_id")),
    )
    op.create_table(
        "digests",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "trigger",
            sa.Enum(
                "scheduled",
                "manual",
                name="digest_trigger",
                native_enum=False,
                create_constraint=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "summarizing",
                "synthesizing",
                "ready",
                "failed",
                name="digest_status",
                native_enum=False,
                create_constraint=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("language", sa.String(length=8), nullable=False),
        sa.Column("voice", sa.String(length=128), nullable=True),
        sa.Column(
            "length",
            sa.Enum(
                "short",
                "normal",
                name="digest_length",
                native_enum=False,
                create_constraint=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("mailbox_ids", postgresql.ARRAY(sa.Uuid()), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("script", sa.Text(), nullable=True),
        sa.Column(
            "references",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column("message_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("todo_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("model", sa.String(length=255), nullable=True),
        sa.Column("prompt_version", sa.String(length=64), nullable=True),
        sa.Column(
            "audio", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint("length IN ('short', 'normal')", name=op.f("ck_digests_digest_length")),
        sa.CheckConstraint(
            "status IN ('pending', 'summarizing', 'synthesizing', 'ready', 'failed')",
            name=op.f("ck_digests_digest_status"),
        ),
        sa.CheckConstraint(
            "trigger IN ('scheduled', 'manual')", name=op.f("ck_digests_digest_trigger")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_digests_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_digests")),
        sa.UniqueConstraint(
            "user_id", "scheduled_for", name=op.f("uq_digests_user_id_scheduled_for")
        ),
    )
    op.create_index(
        "ix_digests_user_id_created_at", "digests", ["user_id", "created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_digests_user_id_created_at", table_name="digests")
    op.drop_table("digests")
    op.drop_table("digest_user_settings")
