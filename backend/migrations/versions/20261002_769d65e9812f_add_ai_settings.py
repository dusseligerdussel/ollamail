"""add ai settings

Revision ID: 769d65e9812f
Revises: 068c341b2ca9
Create Date: 2026-10-02 05:24:03.227056+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "769d65e9812f"
down_revision: str | Sequence[str] | None = "068c341b2ca9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_providers",
        sa.Column("name", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("base_url", sa.String(length=2048), nullable=False),
        # EncryptedStr: envelope-encrypted token (app/core/crypto.py).
        sa.Column("api_key", sa.Text(), nullable=True),
        sa.Column("is_cloud", sa.Boolean(), nullable=False),
        sa.Column("structured_output", sa.String(length=16), nullable=False),
        sa.Column("timeout", sa.Float(), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_providers")),
        sa.UniqueConstraint("name", name=op.f("uq_ai_providers_name")),
    )
    op.create_table(
        "ai_settings",
        sa.Column("singleton", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("cloud_enabled", sa.Boolean(), nullable=True),
        sa.Column("profile", sa.String(length=32), nullable=True),
        sa.Column("concurrency", sa.Integer(), nullable=True),
        sa.Column("tasks", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
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
        sa.CheckConstraint("singleton", name=op.f("ck_ai_settings_singleton")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_settings")),
        sa.UniqueConstraint("singleton", name=op.f("uq_ai_settings_singleton")),
    )


def downgrade() -> None:
    op.drop_table("ai_settings")
    op.drop_table("ai_providers")
