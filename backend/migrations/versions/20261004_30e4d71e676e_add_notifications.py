"""add notifications

Notifications about important mails (#149): ``notification_settings`` holds the opt-in of
each user (off without a row), ``mail_notifications`` the messages already announced, so a
message is announced at most once. IDs and switches only, no mail content.

Revision ID: 30e4d71e676e
Revises: d84e1e94a236
Create Date: 2026-10-04 03:10:41.678598+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "30e4d71e676e"
down_revision: str | Sequence[str] | None = "d84e1e94a236"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_settings",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("category_ids", postgresql.ARRAY(sa.UUID()), server_default="{}", nullable=False),
        sa.Column("show_subject", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("sound", sa.Boolean(), server_default=sa.text("false"), nullable=False),
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
            name=op.f("fk_notification_settings_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_settings")),
        sa.UniqueConstraint("user_id", name=op.f("uq_notification_settings_user_id")),
    )
    op.create_table(
        "mail_notifications",
        sa.Column("message_id", sa.Uuid(), nullable=False),
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
            ["message_id"],
            ["mail_messages.id"],
            name=op.f("fk_mail_notifications_message_id_mail_messages"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mail_notifications")),
        sa.UniqueConstraint("message_id", name=op.f("uq_mail_notifications_message_id")),
    )


def downgrade() -> None:
    op.drop_table("mail_notifications")
    op.drop_table("notification_settings")
