"""Server-side sessions (docs/ARCHITECTURE.md §5, ADR 5: no JWTs in the browser).

The session cookie holds 256 random bits; the database stores their SHA-256. A session ends
at ``expires_at`` (absolute lifetime) or after ``session_idle_timeout_minutes`` without a
request. ``last_seen_at`` is written at most once per ``_TOUCH_INTERVAL`` to keep requests
read-only in the common case.
"""

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import Response
from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import AuthSession
from app.core.config import AuthSettings
from app.users.models import User, UserRole

SESSION_COOKIE = "ollamail_session"
_TOUCH_INTERVAL = timedelta(minutes=1)
_USER_AGENT_LENGTH = 255


@dataclass(frozen=True)
class CurrentSession:
    """The authenticated session of a request."""

    session_id: uuid.UUID
    user_id: uuid.UUID
    role: UserRole

    @property
    def is_admin(self) -> bool:
        return self.role is UserRole.ADMIN


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


async def create_session(
    db: AsyncSession,
    settings: AuthSettings,
    user: User,
    *,
    provider: str,
    user_agent: str | None,
) -> str:
    """Create a session for ``user`` and return the cookie token (caller commits)."""
    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    db.add(
        AuthSession(
            user_id=user.id,
            token_hash=hash_token(token),
            provider=provider,
            expires_at=now + timedelta(minutes=settings.session_lifetime_minutes),
            last_seen_at=now,
            user_agent=user_agent[:_USER_AGENT_LENGTH] if user_agent else None,
        )
    )
    user.last_login_at = now
    await db.flush()
    return token


async def resolve_session(
    db: AsyncSession, settings: AuthSettings, token: str
) -> CurrentSession | None:
    """The valid session for a cookie token, or ``None``. Refreshes ``last_seen_at``."""
    now = datetime.now(UTC)
    idle_cutoff = now - timedelta(minutes=settings.session_idle_timeout_minutes)
    row = (
        await db.execute(
            select(AuthSession.id, AuthSession.last_seen_at, User.id, User.role)
            .join(User, User.id == AuthSession.user_id)
            .where(
                AuthSession.token_hash == hash_token(token),
                AuthSession.expires_at > now,
                AuthSession.last_seen_at > idle_cutoff,
                User.is_active,
            )
        )
    ).one_or_none()
    if row is None:
        return None
    session_id, last_seen_at, user_id, role = row
    if now - last_seen_at >= _TOUCH_INTERVAL:
        await db.execute(
            update(AuthSession).where(AuthSession.id == session_id).values(last_seen_at=now)
        )
        await db.commit()
    return CurrentSession(session_id=session_id, user_id=user_id, role=role)


async def revoke_token(db: AsyncSession, token: str) -> uuid.UUID | None:
    """Delete the session of a cookie token; returns its user ID (``None`` if unknown)."""
    result = await db.execute(
        delete(AuthSession)
        .where(AuthSession.token_hash == hash_token(token))
        .returning(AuthSession.user_id)
    )
    return result.scalar_one_or_none()


async def revoke_session(db: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID) -> bool:
    """Delete one of the user's sessions; ``False`` if it does not exist (or is not theirs)."""
    result = await db.execute(
        delete(AuthSession)
        .where(AuthSession.id == session_id, AuthSession.user_id == user_id)
        .returning(AuthSession.id)
    )
    return result.first() is not None


async def revoke_user_sessions(
    db: AsyncSession, user_id: uuid.UUID, *, keep: uuid.UUID | None = None
) -> int:
    """Delete all sessions of a user, optionally except ``keep``. Returns the count."""
    statement = delete(AuthSession).where(AuthSession.user_id == user_id)
    if keep is not None:
        statement = statement.where(AuthSession.id != keep)
    result = await db.execute(statement.returning(AuthSession.id))
    return len(result.all())


async def list_sessions(
    db: AsyncSession, settings: AuthSettings, user_id: uuid.UUID
) -> list[AuthSession]:
    """The user's valid sessions, most recently used first."""
    now = datetime.now(UTC)
    idle_cutoff = now - timedelta(minutes=settings.session_idle_timeout_minutes)
    result = await db.scalars(
        select(AuthSession)
        .where(
            AuthSession.user_id == user_id,
            AuthSession.expires_at > now,
            AuthSession.last_seen_at > idle_cutoff,
        )
        .order_by(AuthSession.last_seen_at.desc(), AuthSession.id.desc())
    )
    return list(result)


async def purge_expired_sessions(db: AsyncSession, settings: AuthSettings) -> int:
    """Delete expired and idle sessions (housekeeping job)."""
    now = datetime.now(UTC)
    idle_cutoff = now - timedelta(minutes=settings.session_idle_timeout_minutes)
    result = await db.execute(
        delete(AuthSession)
        .where(or_(AuthSession.expires_at <= now, AuthSession.last_seen_at <= idle_cutoff))
        .returning(AuthSession.id)
    )
    return len(result.all())


def set_session_cookie(response: Response, settings: AuthSettings, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_lifetime_minutes * 60,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(response: Response, settings: AuthSettings) -> None:
    response.delete_cookie(
        SESSION_COOKIE, path="/", secure=settings.cookie_secure, httponly=True, samesite="lax"
    )
