"""add todos

Revision ID: 173b28ed6d7e
Revises: 0287a3035915
Create Date: 2026-10-01 20:59:52.310840+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "173b28ed6d7e"
down_revision: str | Sequence[str] | None = "0287a3035915"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The enum checks are the explicit CheckConstraints below (named by the naming convention).
    op.create_table(
        "todos",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("mailbox_id", sa.Uuid(), nullable=True),
        sa.Column("message_id", sa.Uuid(), nullable=True),
        sa.Column("thread_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column(
            "priority",
            sa.Enum(
                "high",
                "normal",
                "low",
                name="todo_priority",
                native_enum=False,
                create_constraint=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "open",
                "done",
                "dismissed",
                name="todo_status",
                native_enum=False,
                create_constraint=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_manual", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("is_edited", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("done_suggested", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "external_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
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
            "priority IN ('high', 'normal', 'low')", name=op.f("ck_todos_todo_priority")
        ),
        sa.CheckConstraint(
            "status IN ('open', 'done', 'dismissed')", name=op.f("ck_todos_todo_status")
        ),
        sa.ForeignKeyConstraint(
            ["mailbox_id"],
            ["mail_mailboxes.id"],
            name=op.f("fk_todos_mailbox_id_mail_mailboxes"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["mail_messages.id"],
            name=op.f("fk_todos_message_id_mail_messages"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["thread_id"],
            ["mail_threads.id"],
            name=op.f("fk_todos_thread_id_mail_threads"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_todos_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_todos")),
    )
    op.create_index(op.f("ix_todos_mailbox_id"), "todos", ["mailbox_id"], unique=False)
    op.create_index(op.f("ix_todos_message_id"), "todos", ["message_id"], unique=False)
    op.create_index(op.f("ix_todos_thread_id"), "todos", ["thread_id"], unique=False)
    op.create_index(
        "ix_todos_user_id_status_due_date", "todos", ["user_id", "status", "due_date"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_todos_user_id_status_due_date", table_name="todos")
    op.drop_index(op.f("ix_todos_thread_id"), table_name="todos")
    op.drop_index(op.f("ix_todos_message_id"), table_name="todos")
    op.drop_index(op.f("ix_todos_mailbox_id"), table_name="todos")
    op.drop_table("todos")
