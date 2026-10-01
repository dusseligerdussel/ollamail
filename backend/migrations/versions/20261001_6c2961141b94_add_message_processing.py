"""add message processing

Revision ID: 6c2961141b94
Revises: d5a9c83a40d8
Create Date: 2026-10-01 20:06:59.247818+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6c2961141b94"
down_revision: str | Sequence[str] | None = "d5a9c83a40d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The status check is the explicit CheckConstraint below (named by the naming convention).
    op.create_table(
        "processing_mailbox_settings",
        sa.Column("mailbox_id", sa.Uuid(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
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
            ["mailbox_id"],
            ["mail_mailboxes.id"],
            name=op.f("fk_processing_mailbox_settings_mailbox_id_mail_mailboxes"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_processing_mailbox_settings")),
        sa.UniqueConstraint("mailbox_id", name=op.f("uq_processing_mailbox_settings_mailbox_id")),
    )
    op.create_table(
        "message_processing",
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("step", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "running",
                "done",
                "failed",
                name="step_status",
                native_enum=False,
                create_constraint=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
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
            "status IN ('pending', 'running', 'done', 'failed')",
            name=op.f("ck_message_processing_step_status"),
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["mail_messages.id"],
            name=op.f("fk_message_processing_message_id_mail_messages"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_message_processing")),
        sa.UniqueConstraint("message_id", "step", name=op.f("uq_message_processing_message_id")),
    )


def downgrade() -> None:
    op.drop_table("message_processing")
    op.drop_table("processing_mailbox_settings")
