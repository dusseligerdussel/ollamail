"""add oidc providers

Revision ID: 068c341b2ca9
Revises: b03b12892021
Create Date: 2026-10-01 21:02:52.263817+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "068c341b2ca9"
down_revision: str | Sequence[str] | None = "b03b12892021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The preset check is an explicit CheckConstraint (named by the naming convention);
    # client_secret holds an EncryptedStr token (TEXT).
    op.create_table(
        "auth_oidc_providers",
        sa.Column("name", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column(
            "preset",
            sa.Enum(
                "generic",
                "entra",
                "google",
                "keycloak",
                "authentik",
                name="oidc_preset",
                native_enum=False,
                create_constraint=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("issuer", sa.String(length=2048), nullable=False),
        sa.Column("client_id", sa.String(length=255), nullable=False),
        sa.Column("client_secret", sa.Text(), nullable=True),
        sa.Column("scopes", postgresql.ARRAY(sa.String(length=64)), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("auto_provision", sa.Boolean(), nullable=False),
        sa.Column("link_by_email", sa.Boolean(), nullable=False),
        sa.Column("allowed_domains", postgresql.ARRAY(sa.String(length=255)), nullable=False),
        sa.Column("groups_claim", sa.String(length=255), nullable=True),
        sa.Column("allowed_tenants", postgresql.ARRAY(sa.String(length=64)), nullable=False),
        sa.Column("hosted_domains", postgresql.ARRAY(sa.String(length=255)), nullable=False),
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
            "preset IN ('generic', 'entra', 'google', 'keycloak', 'authentik')",
            name=op.f("ck_auth_oidc_providers_oidc_preset"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_oidc_providers")),
        sa.UniqueConstraint("name", name=op.f("uq_auth_oidc_providers_name")),
    )
    op.add_column(
        "auth_identities",
        sa.Column(
            "groups", postgresql.ARRAY(sa.String(length=255)), server_default="{}", nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_column("auth_identities", "groups")
    op.drop_table("auth_oidc_providers")
