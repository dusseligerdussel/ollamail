"""SCIM provisioning data (#95): switch, bearer tokens, provisioned users and groups.

Users provisioned by SCIM are ordinary ``users`` rows plus a ``scim_users`` row with the
IdP's ``userName`` and ``externalId``. Their SCIM ID is the user ID. Group memberships live
in ``scim_group_members``; the names of a user's groups are mirrored into an identity with
provider ``scim`` (``auth_identities.groups``), so the group → role mapping (#33) and the
group assignments of shared mailboxes (#34) see them like the groups of any other provider.
Everything that belongs to a user goes with it (``ON DELETE CASCADE``).
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    Table,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class ScimConfig(Base):
    """Instance-wide SCIM settings. At most one row; missing means SCIM is off."""

    __tablename__ = "scim_config"
    __table_args__ = (CheckConstraint("singleton", name="singleton"),)

    singleton: Mapped[bool] = mapped_column(server_default=true(), default=True, unique=True)
    enabled: Mapped[bool] = mapped_column(default=False)
    # Sign-in providers (``oidc:entra``) whose verified e-mail address may link a login to a
    # user created by SCIM, without ``link_by_email`` on the provider itself.
    link_providers: Mapped[list[str]] = mapped_column(
        ARRAY(String(64)), server_default="{}", default=list
    )


class ScimToken(Base):
    """Bearer token of an IdP. Only the SHA-256 is stored; revoking deletes the row. Who
    created it is in the audit log (``idp.config_changed``)."""

    __tablename__ = "scim_tokens"

    name: Mapped[str] = mapped_column(String(100))
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True)
    # First characters of the token, shown so admins can tell tokens apart.
    hint: Mapped[str] = mapped_column(String(16))
    expires_at: Mapped[datetime | None]
    last_used_at: Mapped[datetime | None]


class ScimUser(Base):
    """The IdP's view of a provisioned user."""

    __tablename__ = "scim_users"
    __table_args__ = (
        # userName is unique and compared case-insensitively (RFC 7643 §4.1.1).
        Index("uq_scim_users_user_name_lower", text("lower(user_name)"), unique=True),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )
    user_name: Mapped[str] = mapped_column(String(320))
    external_id: Mapped[str | None] = mapped_column(String(255), index=True)


class ScimGroup(Base):
    """A group pushed by the IdP; its SCIM ID is ``id``."""

    __tablename__ = "scim_groups"

    display_name: Mapped[str] = mapped_column(String(255), index=True)
    external_id: Mapped[str | None] = mapped_column(String(255), index=True)


scim_group_members = Table(
    "scim_group_members",
    Base.metadata,
    Column(
        "group_id",
        ForeignKey("scim_groups.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "user_id",
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    ),
)
