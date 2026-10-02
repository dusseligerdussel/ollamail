"""add rag conversations

Revision ID: aebf78960f69
Revises: 068c341b2ca9
Create Date: 2026-10-02 05:28:59.529494+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "aebf78960f69"
down_revision: str | Sequence[str] | None = "068c341b2ca9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The enum checks are the explicit CheckConstraints below (named by the naming convention).
    op.create_table(
        "rag_conversations",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
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
            name=op.f("fk_rag_conversations_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rag_conversations")),
    )
    op.create_index(
        "ix_rag_conversations_user_id_updated_at",
        "rag_conversations",
        ["user_id", "updated_at"],
        unique=False,
    )
    op.create_table(
        "rag_messages",
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "role",
            sa.Enum(
                "user",
                "assistant",
                name="rag_role",
                native_enum=False,
                create_constraint=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("filters", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "answered",
                "no_evidence",
                name="rag_status",
                native_enum=False,
                create_constraint=False,
                length=16,
            ),
            nullable=True,
        ),
        sa.Column("model", sa.String(length=255), nullable=True),
        sa.Column("prompt_version", sa.String(length=64), nullable=True),
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
        sa.CheckConstraint("role IN ('user', 'assistant')", name=op.f("ck_rag_messages_rag_role")),
        sa.CheckConstraint(
            "status IN ('answered', 'no_evidence')", name=op.f("ck_rag_messages_rag_status")
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["rag_conversations.id"],
            name=op.f("fk_rag_messages_conversation_id_rag_conversations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rag_messages")),
        sa.UniqueConstraint(
            "conversation_id", "position", name=op.f("uq_rag_messages_conversation_id")
        ),
    )
    op.create_table(
        "rag_citations",
        sa.Column("answer_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.SmallInteger(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("mailbox_id", sa.Uuid(), nullable=False),
        sa.Column("attachment_id", sa.Uuid(), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("heading", sa.Text(), nullable=False),
        sa.Column("snippet", sa.Text(), nullable=False),
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
            ["answer_id"],
            ["rag_messages.id"],
            name=op.f("fk_rag_citations_answer_id_rag_messages"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["attachment_id"],
            ["mail_attachments.id"],
            name=op.f("fk_rag_citations_attachment_id_mail_attachments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["mailbox_id"],
            ["mail_mailboxes.id"],
            name=op.f("fk_rag_citations_mailbox_id_mail_mailboxes"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["mail_messages.id"],
            name=op.f("fk_rag_citations_message_id_mail_messages"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rag_citations")),
        sa.UniqueConstraint("answer_id", "number", name=op.f("uq_rag_citations_answer_id")),
    )
    op.create_index(
        op.f("ix_rag_citations_attachment_id"), "rag_citations", ["attachment_id"], unique=False
    )
    op.create_index(
        op.f("ix_rag_citations_mailbox_id"), "rag_citations", ["mailbox_id"], unique=False
    )
    op.create_index(
        op.f("ix_rag_citations_message_id"), "rag_citations", ["message_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_rag_citations_message_id"), table_name="rag_citations")
    op.drop_index(op.f("ix_rag_citations_mailbox_id"), table_name="rag_citations")
    op.drop_index(op.f("ix_rag_citations_attachment_id"), table_name="rag_citations")
    op.drop_table("rag_citations")
    op.drop_table("rag_messages")
    op.drop_index("ix_rag_conversations_user_id_updated_at", table_name="rag_conversations")
    op.drop_table("rag_conversations")
