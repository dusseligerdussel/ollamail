"""add github providers

Revision ID: 8f4d3018c23f
Revises: 068c341b2ca9
Create Date: 2026-10-02 05:21:12.100270+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "8f4d3018c23f"
down_revision: str | Sequence[str] | None = "068c341b2ca9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # client_secret holds an EncryptedStr token (TEXT).
    op.create_table(
        "auth_github_providers",
        sa.Column("name", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("base_url", sa.String(length=2048), nullable=True),
        sa.Column("client_id", sa.String(length=255), nullable=False),
        sa.Column("client_secret", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("auto_provision", sa.Boolean(), nullable=False),
        sa.Column("link_by_email", sa.Boolean(), nullable=False),
        sa.Column("allowed_domains", postgresql.ARRAY(sa.String(length=255)), nullable=False),
        sa.Column("allowed_organizations", postgresql.ARRAY(sa.String(length=64)), nullable=False),
        sa.Column("allowed_teams", postgresql.ARRAY(sa.String(length=255)), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_github_providers")),
        sa.UniqueConstraint("name", name=op.f("uq_auth_github_providers_name")),
    )


def downgrade() -> None:
    op.drop_table("auth_github_providers")
