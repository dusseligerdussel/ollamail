"""add ldap directories

Revision ID: f3eccc01c6d5
Revises: 329b919d6ecb, e41229c08479
Create Date: 2026-10-01 21:03:19.702734+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f3eccc01c6d5"
# Also merges the two heads on main (audit events and search index, both after todos).
down_revision: str | Sequence[str] | None = ("329b919d6ecb", "e41229c08479")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "auth_ldap_directories",
        sa.Column("name", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("settings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        # EncryptedStr: envelope-encrypted token (app/core/crypto.py).
        sa.Column("bind_password", sa.Text(), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_ldap_directories")),
        sa.UniqueConstraint("name", name=op.f("uq_auth_ldap_directories_name")),
    )


def downgrade() -> None:
    op.drop_table("auth_ldap_directories")
