"""The short-lived state between login steps (cookie ``ollamail_mfa``).

After the password the user has no session yet, only this cookie: 256 random bits, the
database stores their SHA-256 (``auth_mfa_pending``). The state belongs to exactly one
user and one login, is valid for ``OLLAMAIL_AUTH_MFA_PENDING_MINUTES``, is deleted when the
login completes, when a new login starts from the same browser and after too many wrong
codes. ``SameSite=Strict`` and ``HttpOnly``; CSRF protection applies as for every request.
"""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import Request, Response
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.mfa.models import PendingLogin
from app.core.config import AuthSettings

PENDING_COOKIE = "ollamail_mfa"

VERIFY = "verify"
ENROLL = "enroll"
PASSKEY = "passkey"
REGISTER = "register"
# Confirming a sensitive action with a passkey (app/auth/reauth.py).
REAUTH = "reauth"

# Wrong codes per pending login; then the password has to be entered again.
MAX_ATTEMPTS = 5


def _hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


async def start(
    db: AsyncSession,
    settings: AuthSettings,
    request: Request,
    response: Response,
    *,
    purpose: str,
    user_id: uuid.UUID | None,
    session_id: uuid.UUID | None = None,
    webauthn_challenge: bytes | None = None,
) -> PendingLogin:
    """Replace this browser's pending state with a new one and set the cookie on
    ``response`` (caller commits)."""
    await discard(db, request)
    token = secrets.token_urlsafe(32)
    pending = PendingLogin(
        token_hash=_hash(token),
        purpose=purpose,
        user_id=user_id,
        session_id=session_id,
        webauthn_challenge=webauthn_challenge,
        attempts=0,
        expires_at=datetime.now(UTC) + timedelta(minutes=settings.mfa_pending_minutes),
    )
    db.add(pending)
    await db.flush()
    response.set_cookie(
        PENDING_COOKIE,
        token,
        max_age=settings.mfa_pending_minutes * 60,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="strict",
    )
    return pending


async def current(
    db: AsyncSession, request: Request, purposes: set[str], *, lock: bool = True
) -> PendingLogin | None:
    """The unexpired state of this browser with one of ``purposes``, row-locked so that
    parallel attempts are counted one after the other."""
    token = request.cookies.get(PENDING_COOKIE)
    if not token:
        return None
    statement = select(PendingLogin).where(
        PendingLogin.token_hash == _hash(token),
        PendingLogin.expires_at > datetime.now(UTC),
        PendingLogin.purpose.in_(purposes),
    )
    if lock:
        statement = statement.with_for_update()
    return await db.scalar(statement)


async def discard(db: AsyncSession, request: Request) -> None:
    token = request.cookies.get(PENDING_COOKIE)
    if token:
        await db.execute(delete(PendingLogin).where(PendingLogin.token_hash == _hash(token)))


def clear_cookie(response: Response, settings: AuthSettings) -> None:
    response.delete_cookie(
        PENDING_COOKIE, path="/", secure=settings.cookie_secure, httponly=True, samesite="strict"
    )


async def purge_expired(db: AsyncSession) -> int:
    result = await db.execute(
        delete(PendingLogin)
        .where(PendingLogin.expires_at <= datetime.now(UTC))
        .returning(PendingLogin.id)
    )
    return len(result.all())
