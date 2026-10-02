"""Second factors of local accounts and the short-lived state between login steps.

Everything hangs on ``users`` via ``ON DELETE CASCADE``. The TOTP secret is encrypted
(``EncryptedStr``); recovery codes and the pending-login token are stored as keyed hashes
or SHA-256 only. Passkeys store the public key and the credential ID, neither is secret.
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, LargeBinary, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.core.crypto import EncryptedStr
from app.core.db import Base


class TotpFactor(Base):
    """TOTP (RFC 6238) of a user. Unconfirmed (``confirmed_at`` null) while being set up."""

    __tablename__ = "auth_mfa_totp"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )
    secret: Mapped[str] = mapped_column(EncryptedStr)
    confirmed_at: Mapped[datetime | None]
    # Time step of the last accepted code: a code is accepted only once (no replay).
    last_used_step: Mapped[int | None] = mapped_column(BigInteger)


class Passkey(Base):
    """A WebAuthn credential: second factor, or passwordless sign-in if discoverable."""

    __tablename__ = "auth_mfa_passkeys"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    credential_id: Mapped[bytes] = mapped_column(LargeBinary(1023), unique=True)
    public_key: Mapped[bytes] = mapped_column(LargeBinary)
    sign_count: Mapped[int] = mapped_column(BigInteger)
    transports: Mapped[list[str]] = mapped_column(
        ARRAY(String(32)), server_default="{}", default=list
    )
    # Chosen by the user ("Laptop", "YubiKey"); shown in Account → Security.
    name: Mapped[str] = mapped_column(String(64))
    # Synced passkey (backup state flag); informational.
    backed_up: Mapped[bool] = mapped_column(default=False)
    last_used_at: Mapped[datetime | None]


class RecoveryCode(Base):
    """One single-use recovery code, stored as keyed hash (``app.auth.mfa.recovery``)."""

    __tablename__ = "auth_mfa_recovery_codes"
    __table_args__ = (UniqueConstraint("user_id", "code_hash"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    code_hash: Mapped[bytes] = mapped_column(LargeBinary(32))
    used_at: Mapped[datetime | None]


class PendingLogin(Base):
    """State between two steps, addressed by the ``ollamail_mfa`` cookie (only its SHA-256
    is stored). Single use, a few minutes valid, bound to one user (except ``passkey``).

    ``purpose``: ``verify`` (password checked, second factor missing), ``enroll``
    (password checked, a factor must be set up first because 2FA is enforced),
    ``passkey`` (passwordless sign-in started, no user yet) and ``register`` (a signed-in
    user registers a passkey).
    """

    __tablename__ = "auth_mfa_pending"

    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True)
    purpose: Mapped[str] = mapped_column(String(16))
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # Session that started a ``register`` flow.
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("auth_sessions.id", ondelete="CASCADE")
    )
    # Challenge of the WebAuthn ceremony in progress, if any.
    webauthn_challenge: Mapped[bytes | None] = mapped_column(LargeBinary(64))
    # Wrong second-factor attempts; the state is dropped at the limit.
    attempts: Mapped[int] = mapped_column(default=0)
    expires_at: Mapped[datetime] = mapped_column(index=True)
