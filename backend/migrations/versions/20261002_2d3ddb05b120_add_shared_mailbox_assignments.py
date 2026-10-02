"""add shared mailbox assignments

Revision ID: 2d3ddb05b120
Revises: d8dd6f4b5840
Create Date: 2026-10-02 06:55:04.689929+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2d3ddb05b120"
down_revision: str | Sequence[str] | None = "d8dd6f4b5840"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The enum check is the explicit CheckConstraint below (named by the naming convention).
    op.create_table(
        "mail_mailbox_assignments",
        sa.Column("mailbox_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("group_name", sa.String(length=255), nullable=True),
        sa.Column("provider", sa.String(length=64), nullable=True),
        sa.Column(
            "permission",
            sa.Enum(
                "read",
                name="assignment_permission",
                native_enum=False,
                create_constraint=False,
                length=16,
            ),
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
            "permission IN ('read')", name=op.f("ck_mail_mailbox_assignments_assignment_permission")
        ),
        sa.CheckConstraint(
            "(user_id IS NULL) <> (group_name IS NULL)",
            name=op.f("ck_mail_mailbox_assignments_principal"),
        ),
        sa.CheckConstraint(
            "provider IS NULL OR group_name IS NOT NULL",
            name=op.f("ck_mail_mailbox_assignments_provider_group"),
        ),
        sa.ForeignKeyConstraint(
            ["mailbox_id"],
            ["mail_mailboxes.id"],
            name=op.f("fk_mail_mailbox_assignments_mailbox_id_mail_mailboxes"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_mail_mailbox_assignments_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mail_mailbox_assignments")),
    )
    op.create_index(
        "ix_mail_mailbox_assignments_group_name",
        "mail_mailbox_assignments",
        [sa.literal_column("lower(group_name)")],
        unique=False,
    )
    op.create_index(
        "ix_mail_mailbox_assignments_user_id", "mail_mailbox_assignments", ["user_id"], unique=False
    )
    op.create_index(
        "uq_mail_mailbox_assignments_group",
        "mail_mailbox_assignments",
        [
            "mailbox_id",
            sa.literal_column("lower(group_name)"),
            sa.literal_column("coalesce(provider, '')"),
        ],
        unique=True,
        postgresql_where=sa.text("group_name IS NOT NULL"),
    )
    op.create_index(
        "uq_mail_mailbox_assignments_user",
        "mail_mailbox_assignments",
        ["mailbox_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )
    op.add_column("todos", sa.Column("assignee_id", sa.Uuid(), nullable=True))
    op.alter_column("todos", "user_id", existing_type=sa.UUID(), nullable=True)
    op.create_index(op.f("ix_todos_assignee_id"), "todos", ["assignee_id"], unique=False)
    op.create_foreign_key(
        op.f("fk_todos_assignee_id_users"),
        "todos",
        "users",
        ["assignee_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        op.f("ck_todos_owner"), "todos", "user_id IS NOT NULL OR mailbox_id IS NOT NULL"
    )
    op.create_check_constraint(
        op.f("ck_todos_assignee"), "todos", "assignee_id IS NULL OR user_id IS NULL"
    )


def downgrade() -> None:
    # Team todos of shared mailboxes have no owner and cannot survive the downgrade.
    op.execute("DELETE FROM todos WHERE user_id IS NULL")
    op.drop_constraint(op.f("ck_todos_assignee"), "todos", type_="check")
    op.drop_constraint(op.f("ck_todos_owner"), "todos", type_="check")
    op.drop_constraint(op.f("fk_todos_assignee_id_users"), "todos", type_="foreignkey")
    op.drop_index(op.f("ix_todos_assignee_id"), table_name="todos")
    op.alter_column("todos", "user_id", existing_type=sa.UUID(), nullable=False)
    op.drop_column("todos", "assignee_id")
    op.drop_index(
        "uq_mail_mailbox_assignments_user",
        table_name="mail_mailbox_assignments",
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )
    op.drop_index(
        "uq_mail_mailbox_assignments_group",
        table_name="mail_mailbox_assignments",
        postgresql_where=sa.text("group_name IS NOT NULL"),
    )
    op.drop_index("ix_mail_mailbox_assignments_user_id", table_name="mail_mailbox_assignments")
    op.drop_index("ix_mail_mailbox_assignments_group_name", table_name="mail_mailbox_assignments")
    op.drop_table("mail_mailbox_assignments")
