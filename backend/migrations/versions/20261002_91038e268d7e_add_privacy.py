"""add privacy: retention settings, data exports and audit log retention

Revision ID: 91038e268d7e
Revises: 769d65e9812f
Create Date: 2026-10-02 06:26:46.911083+00:00

Audit retention is the one documented exception to the append-only audit log
(docs/PRIVACY.md, "Audit-Log"): ``audit_events_purge(cutoff)`` deletes the oldest events
before ``cutoff`` as one contiguous block. It marks the block in a transaction-local
setting, and the trigger lets exactly those ``DELETE``s through; ``UPDATE``, ``TRUNCATE``
and every other ``DELETE`` stay rejected. The newest event is never deleted, so the chain
always has a head to continue from.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "91038e268d7e"
down_revision: str | Sequence[str] | None = "769d65e9812f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Transaction-local setting: events with a smaller ID may be deleted (set by the purge).
PURGE_SETTING = "ollamail.audit_purge_below"

APPEND_ONLY_WITH_RETENTION = f"""
CREATE OR REPLACE FUNCTION audit_events_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    purge_below bigint := NULLIF(current_setting('{PURGE_SETTING}', true), '')::bigint;
BEGIN
    IF TG_OP = 'DELETE' AND purge_below IS NOT NULL AND OLD.id < purge_below THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'audit_events is append-only: % is not allowed', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$
"""

APPEND_ONLY_ORIGINAL = """
CREATE OR REPLACE FUNCTION audit_events_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only: % is not allowed', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$
"""

PURGE_FUNCTION = f"""
CREATE FUNCTION audit_events_purge(cutoff timestamptz) RETURNS bigint
LANGUAGE plpgsql AS $$
DECLARE
    boundary bigint;
    newest bigint;
    deleted bigint;
BEGIN
    -- Same lock as app.audit.service.record: no event is appended meanwhile.
    PERFORM pg_advisory_xact_lock(7022629598041763687);
    SELECT max(id) INTO newest FROM audit_events;
    IF newest IS NULL THEN
        RETURN 0;
    END IF;
    -- First event to keep: the oldest one not before the cutoff, at most the newest.
    SELECT min(id) INTO boundary FROM audit_events WHERE occurred_at >= cutoff;
    boundary := LEAST(COALESCE(boundary, newest), newest);
    PERFORM set_config('{PURGE_SETTING}', boundary::text, true);
    DELETE FROM audit_events WHERE id < boundary;
    GET DIAGNOSTICS deleted = ROW_COUNT;
    PERFORM set_config('{PURGE_SETTING}', '', true);
    RETURN deleted;
END;
$$
"""


def upgrade() -> None:
    # The enum check is the explicit CheckConstraint below (named by the naming convention).
    op.create_table(
        "privacy_retention_settings",
        sa.Column("singleton", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("mail_days", sa.Integer(), nullable=True),
        sa.Column("attachment_days", sa.Integer(), nullable=True),
        sa.Column("search_index_days", sa.Integer(), nullable=True),
        sa.Column("rag_history_days", sa.Integer(), nullable=True),
        sa.Column("digest_days", sa.Integer(), nullable=True),
        sa.Column("audit_days", sa.Integer(), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_run", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
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
        sa.CheckConstraint("singleton", name=op.f("ck_privacy_retention_settings_singleton")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_privacy_retention_settings")),
        sa.UniqueConstraint("singleton", name=op.f("uq_privacy_retention_settings_singleton")),
    )
    op.create_table(
        "privacy_exports",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "running",
                "ready",
                "failed",
                name="privacy_export_status",
                native_enum=False,
                create_constraint=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("file_path", sa.String(length=255), nullable=True),
        sa.Column("size", sa.BigInteger(), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
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
            "status IN ('pending', 'running', 'ready', 'failed')",
            name=op.f("ck_privacy_exports_privacy_export_status"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_privacy_exports_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_privacy_exports")),
    )
    op.create_index(
        op.f("ix_privacy_exports_expires_at"), "privacy_exports", ["expires_at"], unique=False
    )
    op.create_index(
        op.f("ix_privacy_exports_user_id"), "privacy_exports", ["user_id"], unique=False
    )
    op.execute(APPEND_ONLY_WITH_RETENTION)
    op.execute(PURGE_FUNCTION)


def downgrade() -> None:
    op.execute("DROP FUNCTION audit_events_purge(timestamptz)")
    op.execute(APPEND_ONLY_ORIGINAL)
    op.drop_index(op.f("ix_privacy_exports_user_id"), table_name="privacy_exports")
    op.drop_index(op.f("ix_privacy_exports_expires_at"), table_name="privacy_exports")
    op.drop_table("privacy_exports")
    op.drop_table("privacy_retention_settings")
