"""add reply drafts

Revision ID: ff4e75fd2ab8
Revises: 460671df7101
Create Date: 2026-10-02 11:36:30.162669+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "ff4e75fd2ab8"
down_revision: str | Sequence[str] | None = "460671df7101"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The enum check is the explicit CheckConstraint below (named by the naming convention).
    op.create_table(
        "reply_draft_settings",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("signature", sa.Text(), server_default="", nullable=False),
        sa.Column("style_examples", sa.Boolean(), server_default=sa.text("true"), nullable=False),
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
            name=op.f("fk_reply_draft_settings_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reply_draft_settings")),
        sa.UniqueConstraint("user_id", name=op.f("uq_reply_draft_settings_user_id")),
    )
    op.create_table(
        "reply_drafts",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("mailbox_id", sa.Uuid(), nullable=False),
        sa.Column("thread_id", sa.Uuid(), nullable=True),
        sa.Column("message_id", sa.Uuid(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "draft",
                "sent",
                "discarded",
                name="draft_status",
                native_enum=False,
                create_constraint=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("reply_all", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "to", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False
        ),
        sa.Column(
            "cc", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False
        ),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("quote_original", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("instruction", sa.Text(), nullable=True),
        sa.Column("language", sa.String(length=8), nullable=True),
        sa.Column("model", sa.String(length=255), nullable=True),
        sa.Column("prompt_version", sa.String(length=64), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_message_id", sa.Text(), nullable=True),
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
            "status IN ('draft', 'sent', 'discarded')", name=op.f("ck_reply_drafts_draft_status")
        ),
        sa.ForeignKeyConstraint(
            ["mailbox_id"],
            ["mail_mailboxes.id"],
            name=op.f("fk_reply_drafts_mailbox_id_mail_mailboxes"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["mail_messages.id"],
            name=op.f("fk_reply_drafts_message_id_mail_messages"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["thread_id"],
            ["mail_threads.id"],
            name=op.f("fk_reply_drafts_thread_id_mail_threads"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_reply_drafts_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reply_drafts")),
    )
    op.create_index(
        op.f("ix_reply_drafts_mailbox_id"), "reply_drafts", ["mailbox_id"], unique=False
    )
    op.create_index(
        op.f("ix_reply_drafts_message_id"), "reply_drafts", ["message_id"], unique=False
    )
    op.create_index(
        "ix_reply_drafts_user_id_status", "reply_drafts", ["user_id", "status"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_reply_drafts_user_id_status", table_name="reply_drafts")
    op.drop_index(op.f("ix_reply_drafts_message_id"), table_name="reply_drafts")
    op.drop_index(op.f("ix_reply_drafts_mailbox_id"), table_name="reply_drafts")
    op.drop_table("reply_drafts")
    op.drop_table("reply_draft_settings")
