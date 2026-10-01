"""add mail tables

Revision ID: d5a9c83a40d8
Revises: 0001
Create Date: 2026-10-01 19:34:20.346962+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d5a9c83a40d8"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Enum checks are explicit CheckConstraints below (named by the naming convention).
    # credentials: bytea, see app/mail/credentials.py (TODO #6).
    op.create_table(
        "mail_mailboxes",
        sa.Column(
            "type",
            sa.Enum(
                "imap",
                "graph",
                "gmail",
                name="mailbox_type",
                native_enum=False,
                create_constraint=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("address", sa.String(length=320), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=True),
        sa.Column("is_shared", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "provider_settings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("credentials", sa.LargeBinary(), nullable=True),
        sa.Column("sync_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "sync_settings",
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
            "type IN ('imap', 'graph', 'gmail')", name=op.f("ck_mail_mailboxes_mailbox_type")
        ),
        sa.CheckConstraint(
            "(is_shared AND owner_user_id IS NULL)"
            " OR (NOT is_shared AND owner_user_id IS NOT NULL)",
            name=op.f("ck_mail_mailboxes_owner"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mail_mailboxes")),
    )
    op.create_index(
        op.f("ix_mail_mailboxes_owner_user_id"), "mail_mailboxes", ["owner_user_id"], unique=False
    )
    op.create_table(
        "mail_folders",
        sa.Column("mailbox_id", sa.Uuid(), nullable=False),
        sa.Column("remote_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "folder",
                "label",
                name="folder_kind",
                native_enum=False,
                create_constraint=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column(
            "role",
            sa.Enum(
                "inbox",
                "sent",
                "drafts",
                "trash",
                "junk",
                "archive",
                "all",
                name="folder_role",
                native_enum=False,
                create_constraint=False,
                length=16,
            ),
            nullable=True,
        ),
        sa.Column("sync_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
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
        sa.CheckConstraint("kind IN ('folder', 'label')", name=op.f("ck_mail_folders_folder_kind")),
        sa.CheckConstraint(
            "role IN ('inbox', 'sent', 'drafts', 'trash', 'junk', 'archive', 'all')",
            name=op.f("ck_mail_folders_folder_role"),
        ),
        sa.ForeignKeyConstraint(
            ["mailbox_id"],
            ["mail_mailboxes.id"],
            name=op.f("fk_mail_folders_mailbox_id_mail_mailboxes"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mail_folders")),
        sa.UniqueConstraint("mailbox_id", "remote_id", name=op.f("uq_mail_folders_mailbox_id")),
    )
    op.create_table(
        "mail_threads",
        sa.Column("mailbox_id", sa.Uuid(), nullable=False),
        sa.Column("provider_thread_id", sa.Text(), nullable=True),
        sa.Column("subject_key", sa.Text(), nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
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
            name=op.f("fk_mail_threads_mailbox_id_mail_mailboxes"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mail_threads")),
    )
    op.create_index(
        "ix_mail_threads_mailbox_id_provider_thread_id",
        "mail_threads",
        ["mailbox_id", "provider_thread_id"],
        unique=False,
    )
    op.create_index(
        "ix_mail_threads_mailbox_id_subject_key",
        "mail_threads",
        ["mailbox_id", "subject_key"],
        unique=False,
    )
    op.create_table(
        "mail_messages",
        sa.Column("mailbox_id", sa.Uuid(), nullable=False),
        sa.Column("thread_id", sa.Uuid(), nullable=True),
        sa.Column("remote_ref", sa.Text(), nullable=False),
        sa.Column("message_id_header", sa.Text(), nullable=True),
        sa.Column("in_reply_to", sa.Text(), nullable=True),
        sa.Column("references", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("sender", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "to", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False
        ),
        sa.Column(
            "cc", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False
        ),
        sa.Column(
            "bcc", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False
        ),
        sa.Column(
            "reply_to", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False
        ),
        sa.Column(
            "headers", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("body_html", sa.Text(), nullable=True),
        sa.Column("body_main", sa.Text(), nullable=False),
        sa.Column("signature", sa.Text(), nullable=True),
        sa.Column("language", sa.String(length=8), nullable=True),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("flags", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column("has_attachments", sa.Boolean(), server_default=sa.text("false"), nullable=False),
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
            name=op.f("fk_mail_messages_mailbox_id_mail_mailboxes"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["thread_id"],
            ["mail_threads.id"],
            name=op.f("fk_mail_messages_thread_id_mail_threads"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mail_messages")),
        sa.UniqueConstraint("mailbox_id", "remote_ref", name=op.f("uq_mail_messages_mailbox_id")),
    )
    op.create_index(
        "ix_mail_messages_mailbox_id_message_id_header",
        "mail_messages",
        ["mailbox_id", "message_id_header"],
        unique=False,
    )
    op.create_index(
        op.f("ix_mail_messages_thread_id"), "mail_messages", ["thread_id"], unique=False
    )
    op.create_table(
        "mail_sync_states",
        sa.Column("mailbox_id", sa.Uuid(), nullable=False),
        sa.Column("folder_id", sa.Uuid(), nullable=True),
        sa.Column(
            "cursor", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["folder_id"],
            ["mail_folders.id"],
            name=op.f("fk_mail_sync_states_folder_id_mail_folders"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["mailbox_id"],
            ["mail_mailboxes.id"],
            name=op.f("fk_mail_sync_states_mailbox_id_mail_mailboxes"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mail_sync_states")),
        sa.UniqueConstraint(
            "mailbox_id",
            "folder_id",
            name=op.f("uq_mail_sync_states_mailbox_id"),
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_table(
        "mail_attachments",
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("mailbox_id", sa.Uuid(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=True),
        sa.Column("content_type", sa.String(length=255), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("content_id", sa.Text(), nullable=True),
        sa.Column("is_inline", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
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
            name=op.f("fk_mail_attachments_mailbox_id_mail_mailboxes"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["mail_messages.id"],
            name=op.f("fk_mail_attachments_message_id_mail_messages"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mail_attachments")),
    )
    op.create_index(
        op.f("ix_mail_attachments_mailbox_id"), "mail_attachments", ["mailbox_id"], unique=False
    )
    op.create_index(
        op.f("ix_mail_attachments_message_id"), "mail_attachments", ["message_id"], unique=False
    )
    op.create_table(
        "mail_message_folders",
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("folder_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["folder_id"],
            ["mail_folders.id"],
            name=op.f("fk_mail_message_folders_folder_id_mail_folders"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["mail_messages.id"],
            name=op.f("fk_mail_message_folders_message_id_mail_messages"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("message_id", "folder_id", name=op.f("pk_mail_message_folders")),
    )
    op.create_index(
        op.f("ix_mail_message_folders_folder_id"),
        "mail_message_folders",
        ["folder_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_mail_message_folders_folder_id"), table_name="mail_message_folders")
    op.drop_table("mail_message_folders")
    op.drop_index(op.f("ix_mail_attachments_message_id"), table_name="mail_attachments")
    op.drop_index(op.f("ix_mail_attachments_mailbox_id"), table_name="mail_attachments")
    op.drop_table("mail_attachments")
    op.drop_table("mail_sync_states")
    op.drop_index(op.f("ix_mail_messages_thread_id"), table_name="mail_messages")
    op.drop_index("ix_mail_messages_mailbox_id_message_id_header", table_name="mail_messages")
    op.drop_table("mail_messages")
    op.drop_index("ix_mail_threads_mailbox_id_subject_key", table_name="mail_threads")
    op.drop_index("ix_mail_threads_mailbox_id_provider_thread_id", table_name="mail_threads")
    op.drop_table("mail_threads")
    op.drop_table("mail_folders")
    op.drop_index(op.f("ix_mail_mailboxes_owner_user_id"), table_name="mail_mailboxes")
    op.drop_table("mail_mailboxes")
