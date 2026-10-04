"""add push subscriptions

Web Push (#181): ``push_subscriptions`` holds the devices of a user that receive
notifications without an open tab. Endpoint and keys are encrypted (``EncryptedJSON``),
``endpoint_hash`` finds a browser again; deleted with the user.

Revision ID: 4adf661e4588
Revises: 30e4d71e676e
Create Date: 2026-10-04 07:50:17.379932+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4adf661e4588"
down_revision: str | Sequence[str] | None = "30e4d71e676e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # subscription holds an EncryptedJSON token (TEXT).
    op.create_table(
        "push_subscriptions",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("endpoint_hash", sa.String(length=64), nullable=False),
        sa.Column("subscription", sa.Text(), nullable=False),
        sa.Column("browser", sa.String(length=32), nullable=True),
        sa.Column("os", sa.String(length=32), nullable=True),
        sa.Column("mobile", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
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
            name=op.f("fk_push_subscriptions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_push_subscriptions")),
        sa.UniqueConstraint("endpoint_hash", name=op.f("uq_push_subscriptions_endpoint_hash")),
    )
    op.create_index(
        op.f("ix_push_subscriptions_user_id"), "push_subscriptions", ["user_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_push_subscriptions_user_id"), table_name="push_subscriptions")
    op.drop_table("push_subscriptions")
