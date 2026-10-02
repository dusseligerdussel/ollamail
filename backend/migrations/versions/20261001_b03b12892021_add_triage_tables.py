"""add triage tables

Revision ID: b03b12892021
Revises: f3eccc01c6d5
Create Date: 2026-10-01 21:02:22.346959+00:00
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b03b12892021"
down_revision: str | Sequence[str] | None = "f3eccc01c6d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Organisation defaults (docs/ARCHITECTURE.md §4.2). A snapshot of
# ``app.triage.categories.DEFAULT_CATEGORIES``: later changes there need a new migration.
DEFAULT_CATEGORIES = [
    (
        "important",
        "Important",
        "Personal mail that matters to the recipient and should be read soon, e.g. from "
        "colleagues, customers or family, or about deadlines, contracts, money or health.",
    ),
    (
        "action_required",
        "Action required",
        "The sender expects the recipient to do something: reply, decide, approve, pay, "
        "sign, fill in a form or schedule a meeting.",
    ),
    (
        "waiting_for",
        "Waiting for",
        "Answers or updates on something the recipient started and is waiting for, e.g. "
        "replies to the recipient's questions, order or booking confirmations, status "
        "updates on open requests.",
    ),
    (
        "info",
        "Info",
        "Personal or work mail that only informs and needs no action, e.g. FYI messages, "
        "meeting minutes, announcements to a team.",
    ),
    (
        "newsletter",
        "Newsletter",
        "Newsletters, mailing lists, digests, blogs and other subscribed bulk content.",
    ),
    (
        "notification",
        "Notification",
        "Automated messages from systems and services, e.g. account and security alerts, "
        "shipping updates, invoices sent automatically, calendar, ticket or build "
        "notifications.",
    ),
    (
        "spam",
        "Spam/Advertising",
        "Unsolicited advertising, marketing offers, scams, phishing and other spam.",
    ),
]


def upgrade() -> None:
    # The enum checks are the explicit CheckConstraints below (named by the naming convention).
    op.create_table(
        "triage_categories",
        sa.Column("owner_user_id", sa.Uuid(), nullable=True),
        sa.Column("builtin_key", sa.String(length=32), nullable=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), server_default="0", nullable=False),
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
            ["owner_user_id"],
            ["users.id"],
            name=op.f("fk_triage_categories_owner_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_triage_categories")),
    )
    op.create_index(
        op.f("ix_triage_categories_owner_user_id"),
        "triage_categories",
        ["owner_user_id"],
        unique=False,
    )
    op.create_table(
        "triage_category_preferences",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("category_id", sa.Uuid(), nullable=False),
        sa.Column("hidden", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=True),
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
            ["category_id"],
            ["triage_categories.id"],
            name=op.f("fk_triage_category_preferences_category_id_triage_categories"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_triage_category_preferences_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_triage_category_preferences")),
        sa.UniqueConstraint(
            "user_id", "category_id", name=op.f("uq_triage_category_preferences_user_id")
        ),
    )
    op.create_index(
        op.f("ix_triage_category_preferences_category_id"),
        "triage_category_preferences",
        ["category_id"],
        unique=False,
    )
    op.create_table(
        "triage_mailbox_settings",
        sa.Column("mailbox_id", sa.Uuid(), nullable=False),
        sa.Column(
            "write_back",
            sa.Enum(
                "off",
                "label",
                "move",
                name="triage_write_back",
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
            "write_back IN ('off', 'label', 'move')",
            name=op.f("ck_triage_mailbox_settings_triage_write_back"),
        ),
        sa.ForeignKeyConstraint(
            ["mailbox_id"],
            ["mail_mailboxes.id"],
            name=op.f("fk_triage_mailbox_settings_mailbox_id_mail_mailboxes"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_triage_mailbox_settings")),
        sa.UniqueConstraint("mailbox_id", name=op.f("uq_triage_mailbox_settings_mailbox_id")),
    )
    op.create_table(
        "triage_sender_rules",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("sender", sa.String(length=320), nullable=False),
        sa.Column("category_id", sa.Uuid(), nullable=False),
        sa.Column("priority", sa.SmallInteger(), nullable=False),
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
            "priority BETWEEN 1 AND 3", name=op.f("ck_triage_sender_rules_priority")
        ),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["triage_categories.id"],
            name=op.f("fk_triage_sender_rules_category_id_triage_categories"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_triage_sender_rules_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_triage_sender_rules")),
        sa.UniqueConstraint("user_id", "sender", name=op.f("uq_triage_sender_rules_user_id")),
    )
    op.create_index(
        op.f("ix_triage_sender_rules_category_id"),
        "triage_sender_rules",
        ["category_id"],
        unique=False,
    )
    op.create_table(
        "triage_feedback",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("category_id", sa.Uuid(), nullable=False),
        sa.Column("priority", sa.SmallInteger(), nullable=False),
        sa.Column("embedding", postgresql.ARRAY(sa.Float()), nullable=True),
        sa.Column("embedding_model", sa.String(length=255), nullable=True),
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
        sa.CheckConstraint("priority BETWEEN 1 AND 3", name=op.f("ck_triage_feedback_priority")),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["triage_categories.id"],
            name=op.f("fk_triage_feedback_category_id_triage_categories"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["mail_messages.id"],
            name=op.f("fk_triage_feedback_message_id_mail_messages"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_triage_feedback_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_triage_feedback")),
        sa.UniqueConstraint("user_id", "message_id", name=op.f("uq_triage_feedback_user_id")),
    )
    op.create_index(
        op.f("ix_triage_feedback_category_id"), "triage_feedback", ["category_id"], unique=False
    )
    op.create_index(
        op.f("ix_triage_feedback_message_id"), "triage_feedback", ["message_id"], unique=False
    )
    op.create_table(
        "triage_results",
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("category_id", sa.Uuid(), nullable=True),
        sa.Column("priority", sa.SmallInteger(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("rule", sa.String(length=32), nullable=True),
        sa.Column(
            "source",
            sa.Enum(
                "rule",
                "sender_rule",
                "llm",
                "user",
                name="triage_source",
                native_enum=False,
                create_constraint=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("model", sa.String(length=255), nullable=True),
        sa.Column("prompt_version", sa.String(length=64), nullable=True),
        sa.Column("remote_label", sa.Text(), nullable=True),
        sa.Column(
            "write_back_pending", sa.Boolean(), server_default=sa.text("false"), nullable=False
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
            "source IN ('rule', 'sender_rule', 'llm', 'user')",
            name=op.f("ck_triage_results_triage_source"),
        ),
        sa.CheckConstraint("priority BETWEEN 1 AND 3", name=op.f("ck_triage_results_priority")),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["triage_categories.id"],
            name=op.f("fk_triage_results_category_id_triage_categories"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["mail_messages.id"],
            name=op.f("fk_triage_results_message_id_mail_messages"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_triage_results")),
        sa.UniqueConstraint("message_id", name=op.f("uq_triage_results_message_id")),
    )
    op.create_index(
        op.f("ix_triage_results_category_id"), "triage_results", ["category_id"], unique=False
    )
    op.create_index(
        "ix_triage_results_write_back_pending",
        "triage_results",
        ["updated_at"],
        unique=False,
        postgresql_where=sa.text("write_back_pending"),
    )

    categories = sa.table(
        "triage_categories",
        sa.column("id", sa.Uuid()),
        sa.column("builtin_key", sa.String()),
        sa.column("name", sa.String()),
        sa.column("description", sa.Text()),
        sa.column("position", sa.Integer()),
    )
    op.bulk_insert(
        categories,
        [
            {
                "id": uuid.uuid4(),
                "builtin_key": key,
                "name": name,
                "description": description,
                "position": position,
            }
            for position, (key, name, description) in enumerate(DEFAULT_CATEGORIES)
        ],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_triage_results_write_back_pending",
        table_name="triage_results",
        postgresql_where=sa.text("write_back_pending"),
    )
    op.drop_index(op.f("ix_triage_results_category_id"), table_name="triage_results")
    op.drop_table("triage_results")
    op.drop_index(op.f("ix_triage_feedback_message_id"), table_name="triage_feedback")
    op.drop_index(op.f("ix_triage_feedback_category_id"), table_name="triage_feedback")
    op.drop_table("triage_feedback")
    op.drop_index(op.f("ix_triage_sender_rules_category_id"), table_name="triage_sender_rules")
    op.drop_table("triage_sender_rules")
    op.drop_table("triage_mailbox_settings")
    op.drop_index(
        op.f("ix_triage_category_preferences_category_id"), table_name="triage_category_preferences"
    )
    op.drop_table("triage_category_preferences")
    op.drop_index(op.f("ix_triage_categories_owner_user_id"), table_name="triage_categories")
    op.drop_table("triage_categories")
