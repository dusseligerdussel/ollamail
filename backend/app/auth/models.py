"""Authentication data: identities, server-side sessions and rate-limit counters."""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

LOCAL_PROVIDER = "local"


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
