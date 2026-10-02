"""add mfa

Revision ID: 1efbd562bd72
Revises: 460671df7101
Create Date: 2026-10-02 11:33:56.359920+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "1efbd562bd72"
down_revision: str | Sequence[str] | None = "460671df7101"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # auth_mfa_totp.secret holds an EncryptedStr token (TEXT). The enum check of
    # auth_policy.mfa_enforcement is the explicit CheckConstraint below.
    op.create_table(
        "auth_mfa_passkeys",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("credential_id", sa.LargeBinary(length=1023), nullable=False),
        sa.Column("public_key", sa.LargeBinary(), nullable=False),
        sa.Column("sign_count", sa.BigInteger(), nullable=False),
        sa.Column(
            "transports",
            postgresql.ARRAY(sa.String(length=32)),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("backed_up", sa.Boolean(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
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
            name=op.f("fk_auth_mfa_passkeys_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_mfa_passkeys")),
        sa.UniqueConstraint("credential_id", name=op.f("uq_auth_mfa_passkeys_credential_id")),
    )
    op.create_index(
        op.f("ix_auth_mfa_passkeys_user_id"), "auth_mfa_passkeys", ["user_id"], unique=False
    )
    op.create_table(
        "auth_mfa_recovery_codes",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("code_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
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
            name=op.f("fk_auth_mfa_recovery_codes_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_mfa_recovery_codes")),
        sa.UniqueConstraint(
            "user_id", "code_hash", name=op.f("uq_auth_mfa_recovery_codes_user_id")
        ),
    )
    op.create_index(
        op.f("ix_auth_mfa_recovery_codes_user_id"),
        "auth_mfa_recovery_codes",
        ["user_id"],
        unique=False,
    )
    op.create_table(
        "auth_mfa_totp",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("secret", sa.Text(), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_step", sa.BigInteger(), nullable=True),
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
            name=op.f("fk_auth_mfa_totp_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_mfa_totp")),
        sa.UniqueConstraint("user_id", name=op.f("uq_auth_mfa_totp_user_id")),
    )
    op.create_table(
        "auth_mfa_pending",
        sa.Column("token_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("purpose", sa.String(length=16), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("session_id", sa.Uuid(), nullable=True),
        sa.Column("webauthn_challenge", sa.LargeBinary(length=64), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
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
            ["session_id"],
            ["auth_sessions.id"],
            name=op.f("fk_auth_mfa_pending_session_id_auth_sessions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_auth_mfa_pending_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_mfa_pending")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_auth_mfa_pending_token_hash")),
    )
    op.create_index(
        op.f("ix_auth_mfa_pending_expires_at"), "auth_mfa_pending", ["expires_at"], unique=False
    )
    op.create_index(
        op.f("ix_auth_mfa_pending_user_id"), "auth_mfa_pending", ["user_id"], unique=False
    )
    op.add_column(
        "auth_policy",
        sa.Column(
            "mfa_enforcement",
            sa.Enum(
                "off",
                "admins",
                "all",
                name="mfa_enforcement",
                native_enum=False,
                create_constraint=False,
                length=16,
            ),
            server_default="off",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_auth_policy_mfa_enforcement"),
        "auth_policy",
        "mfa_enforcement IN ('off', 'admins', 'all')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_auth_policy_mfa_enforcement"), "auth_policy", type_="check")
    op.drop_column("auth_policy", "mfa_enforcement")
    op.drop_index(op.f("ix_auth_mfa_pending_user_id"), table_name="auth_mfa_pending")
    op.drop_index(op.f("ix_auth_mfa_pending_expires_at"), table_name="auth_mfa_pending")
    op.drop_table("auth_mfa_pending")
    op.drop_table("auth_mfa_totp")
    op.drop_index(op.f("ix_auth_mfa_recovery_codes_user_id"), table_name="auth_mfa_recovery_codes")
    op.drop_table("auth_mfa_recovery_codes")
    op.drop_index(op.f("ix_auth_mfa_passkeys_user_id"), table_name="auth_mfa_passkeys")
    op.drop_table("auth_mfa_passkeys")
