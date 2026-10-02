"""add saml providers

Revision ID: c49022361cc3
Revises: 2d3ddb05b120
Create Date: 2026-10-02 11:34:02.172175+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c49022361cc3"
down_revision: str | Sequence[str] | None = "2d3ddb05b120"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nothing secret: IdP certificates are public keys; assertions holds only SHA-256 keys.
    op.create_table(
        "auth_saml_assertions",
        sa.Column("key", sa.LargeBinary(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_auth_saml_assertions")),
    )
    op.create_index(
        op.f("ix_auth_saml_assertions_expires_at"),
        "auth_saml_assertions",
        ["expires_at"],
        unique=False,
    )
    op.create_table(
        "auth_saml_providers",
        sa.Column("name", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("preset", sa.String(length=32), nullable=False),
        sa.Column("metadata_url", sa.String(length=2048), nullable=True),
        sa.Column("metadata_refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idp_entity_id", sa.String(length=1024), nullable=False),
        sa.Column("idp_sso_url", sa.String(length=2048), nullable=False),
        sa.Column("idp_certificates", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("sp_entity_id", sa.String(length=1024), nullable=True),
        sa.Column("name_id_format", sa.String(length=128), nullable=False),
        sa.Column("subject_attribute", sa.String(length=255), nullable=True),
        sa.Column("email_attribute", sa.String(length=255), nullable=True),
        sa.Column("display_name_attribute", sa.String(length=255), nullable=True),
        sa.Column("groups_attribute", sa.String(length=255), nullable=True),
        sa.Column("trust_email", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("auto_provision", sa.Boolean(), nullable=False),
        sa.Column("link_by_email", sa.Boolean(), nullable=False),
        sa.Column("allowed_domains", postgresql.ARRAY(sa.String(length=255)), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_saml_providers")),
        sa.UniqueConstraint("name", name=op.f("uq_auth_saml_providers_name")),
    )


def downgrade() -> None:
    op.drop_table("auth_saml_providers")
    op.drop_index(op.f("ix_auth_saml_assertions_expires_at"), table_name="auth_saml_assertions")
    op.drop_table("auth_saml_assertions")
