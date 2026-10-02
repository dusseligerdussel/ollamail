"""add todo export targets

Revision ID: 2183db5869b4
Revises: 2d3ddb05b120
Create Date: 2026-10-02 11:32:54.789317+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2183db5869b4"
down_revision: str | Sequence[str] | None = "2d3ddb05b120"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The mode check is the explicit CheckConstraint below (named by the naming convention);
    # config and pending_deletions hold EncryptedJSON tokens (TEXT).
    op.create_table(
        "todo_export_targets",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("sink", sa.String(length=32), nullable=False),
        sa.Column("config", sa.Text(), nullable=False),
        sa.Column("list_id", sa.String(length=2048), nullable=False),
        sa.Column("list_name", sa.String(length=255), nullable=False),
        sa.Column(
            "mode",
            sa.Enum(
                "auto",
                "manual",
                name="todo_export_mode",
                native_enum=False,
                create_constraint=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("app_url", sa.String(length=255), nullable=True),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pending_deletions", sa.Text(), nullable=True),
        sa.Column("last_error", sa.String(length=64), nullable=True),
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
            "mode IN ('auto', 'manual')", name=op.f("ck_todo_export_targets_todo_export_mode")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_todo_export_targets_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_todo_export_targets")),
        sa.UniqueConstraint("user_id", name=op.f("uq_todo_export_targets_user_id")),
    )


def downgrade() -> None:
    op.drop_table("todo_export_targets")
