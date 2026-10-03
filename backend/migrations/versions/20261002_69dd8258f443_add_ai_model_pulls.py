"""add ai model pulls

Revision ID: 69dd8258f443
Revises: 664d0bf2fc5c
Create Date: 2026-10-02 23:05:18.003430+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "69dd8258f443"
down_revision: str | Sequence[str] | None = "664d0bf2fc5c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_model_pulls",
        sa.Column("endpoint", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("completed", sa.BigInteger(), nullable=False),
        sa.Column("total", sa.BigInteger(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_model_pulls")),
        sa.UniqueConstraint("endpoint", "model", name=op.f("uq_ai_model_pulls_endpoint")),
    )


def downgrade() -> None:
    op.drop_table("ai_model_pulls")
