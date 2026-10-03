"""Authentication data: identities, server-side sessions and rate-limit counters."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    true,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.users.models import UserRole

LOCAL_PROVIDER = "local"
# Identity maintained by SCIM provisioning (app/scim): carries the user's SCIM groups, not a
# way to sign in.
SCIM_PROVIDER = "scim"


class Identity(Base):
    """How a user signs in: one row per (provider, subject).

    ``provider`` names a configured auth provider (``local``, later e.g. ``oidc:entra``,
    ``github``, ``ldap:corp``), ``subject`` is the stable user ID at that provider
    (OIDC ``sub``, GitHub user ID, LDAP DN/objectGUID). For ``local`` the subject is the
    user ID and ``password_hash`` holds the Argon2id hash.
    """

    __tablename__ = "auth_identities"
    __table_args__ = (
        UniqueConstraint("provider", "subject"),
        CheckConstraint(
            f"password_hash IS NULL OR provider = '{LOCAL_PROVIDER}'", name="password_local"
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(64))
    subject: Mapped[str] = mapped_column(String(255))
    password_hash: Mapped[str | None] = mapped_column(Text)
    last_used_at: Mapped[datetime | None]
    # Groups the provider reported at the last login (OIDC groups claim, LDAP groups,
    # GitHub teams); input for the group → role mapping (#33).
    groups: Mapped[list[str]] = mapped_column(ARRAY(String(255)), server_default="{}", default=list)


class AuthSession(Base):
    """A server-side login session. The cookie carries a random token; only its SHA-256
    is stored, so a database dump does not contain usable sessions. Revoking a session
    deletes the row."""

    __tablename__ = "auth_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True)
    # Provider of the identity used to sign in (``local``, ``oidc:entra``, ...).
    provider: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(index=True)
    last_seen_at: Mapped[datetime]
    # Last time the user proved who they are in this session: the sign-in itself or a
    # confirmation before a sensitive action (app/auth/reauth.py).
    authenticated_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # Shown in the session list so users can recognise their devices; truncated.
    user_agent: Mapped[str | None] = mapped_column(String(255))


# Fixed-window counters for login rate limits and lockouts. Keys contain only keyed
# hashes of IP addresses and e-mail addresses, never the values themselves.
rate_limits = Table(
    "auth_rate_limits",
    Base.metadata,
    Column("key", String(128), primary_key=True),
    Column("window_start", DateTime(timezone=True), nullable=False, index=True),
    Column("hits", Integer, nullable=False),
)


def _role_column() -> Enum:
    # Same representation as ``users.role`` (VARCHAR + CHECK).
    return Enum(
        UserRole,
        name="user_role",
        native_enum=False,
        create_constraint=True,
        length=16,
        values_callable=lambda members: [member.value for member in members],
    )


class MfaEnforcement(enum.StrEnum):
    """Which local accounts must use a second factor (app/auth/mfa)."""

    OFF = "off"
    ADMINS = "admins"
    ALL = "all"


class AuthPolicy(Base):
    """Instance-wide sign-in settings (admin UI, #33). At most one row; missing means
    defaults (``app.auth.policy.get_policy``)."""

    __tablename__ = "auth_policy"
    __table_args__ = (CheckConstraint("singleton", name="singleton"),)

    # Always true; the unique constraint allows only one row.
    singleton: Mapped[bool] = mapped_column(server_default=true(), default=True, unique=True)
    # Sign-in with local accounts (e-mail + password). Can only be switched off while
    # another admin access works (app.auth.admin_access).
    local_login_enabled: Mapped[bool] = mapped_column(server_default=true(), default=True)
    # Derive the role of external users from their groups at every login.
    role_mapping_enabled: Mapped[bool] = mapped_column(default=False)
    # Role of external users no mapping rule matches (role mapping on).
    default_role: Mapped[UserRole] = mapped_column(_role_column(), default=UserRole.USER)
    # Local accounts without a second factor must set one up at their next login (#96).
    mfa_enforcement: Mapped[MfaEnforcement] = mapped_column(
        Enum(
            MfaEnforcement,
            name="mfa_enforcement",
            native_enum=False,
            create_constraint=True,
            length=16,
            values_callable=lambda members: [member.value for member in members],
        ),
        server_default=MfaEnforcement.OFF.value,
        default=MfaEnforcement.OFF,
    )


class RoleMappingRule(Base):
    """Group → role rule: members of ``group`` (at ``provider``, or any provider if null)
    get ``role``. The highest matching role wins; group names compare case-insensitively.
    """

    __tablename__ = "auth_role_mapping_rules"

    # Group as the provider reports it: Entra group object ID, LDAP group DN, OIDC group name.
    group: Mapped[str] = mapped_column(String(255))
    # Provider key (``oidc:entra``, ``ldap:corp``); null applies to all providers.
    provider: Mapped[str | None] = mapped_column(String(64))
    role: Mapped[UserRole] = mapped_column(_role_column())


class Invitation(Base):
    """Invitation link for a local account without password. Only the SHA-256 of the
    token is stored; accepting it sets the password and deletes the row."""

    __tablename__ = "auth_invitations"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True)
    expires_at: Mapped[datetime] = mapped_column(index=True)
