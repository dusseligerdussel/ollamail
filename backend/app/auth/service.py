"""Login flow shared by all providers: throttling, identity → user, session start."""

from datetime import timedelta

from fastapi import Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.auth import rate_limit
from app.auth.csrf import set_csrf_cookie
from app.auth.keys import derive_key, keyed_digest
from app.auth.models import Identity
from app.auth.providers import VerifiedIdentity
from app.auth.sessions import (
    SESSION_COOKIE,
    clear_session_cookie,
    create_session,
    revoke_token,
    set_session_cookie,
)
from app.core.config import Settings
from app.core.errors import ProblemError
from app.users.models import User


def _client_ip(request: Request) -> str:
    # With uvicorn --proxy-headers/--forwarded-allow-ips this is the X-Forwarded-For client.
    return request.client.host if request.client else "unknown"


def _too_many(retry_after: int) -> ProblemError:
    return ProblemError(
        429,
        detail="Too many attempts. Try again later.",
        type="urn:ollamail:problem:too-many-attempts",
        retry_after=retry_after,
    )


async def throttle_ip(db: AsyncSession, settings: Settings, request: Request) -> None:
    """Count an attempt for the client IP; 429 above ``ip_max_attempts`` per window."""
    key = derive_key(settings.security, "rate-limit")
    window = timedelta(minutes=settings.auth.login_window_minutes)
    hit = await rate_limit.hit(db, "ip:" + keyed_digest(key, _client_ip(request)), window)
    await db.commit()
    if hit.count > settings.auth.ip_max_attempts:
        raise _too_many(hit.retry_after())


def account_key(settings: Settings, login: str) -> str:
    key = derive_key(settings.security, "rate-limit")
    return "account:" + keyed_digest(key, login.strip().lower())


async def throttle_account(db: AsyncSession, settings: Settings, login: str) -> None:
    """Count a login attempt for the account; 429 (locked) above ``login_max_attempts``.

    Counted before the password check and committed right away, so parallel guesses
    cannot exceed the limit. Works the same for unknown accounts (no enumeration).
    """
    window = timedelta(minutes=settings.auth.login_window_minutes)
    hit = await rate_limit.hit(db, account_key(settings, login), window)
    await db.commit()
    if hit.count > settings.auth.login_max_attempts:
        raise _too_many(hit.retry_after())


async def reset_account_throttle(db: AsyncSession, settings: Settings, login: str) -> None:
    await rate_limit.reset(db, account_key(settings, login))


async def user_for_identity(db: AsyncSession, identity: VerifiedIdentity) -> User | None:
    """The active user linked to a verified identity (no provisioning).

    External providers use ``app.auth.provisioning.provision_user`` instead, which also
    creates or links users.
    """
    user = await db.scalar(
        select(User)
        .join(Identity, Identity.user_id == User.id)
        .where(Identity.provider == identity.provider, Identity.subject == identity.subject)
    )
    if user is None or not user.is_active:
        return None
    return user


async def start_session(
    db: AsyncSession,
    settings: Settings,
    request: Request,
    response: Response,
    user: User,
    *,
    provider: str,
) -> None:
    """Replace the request's session (if any) with a new one and set both cookies."""
    previous = request.cookies.get(SESSION_COOKIE)
    if previous:
        await revoke_token(db, previous)
    token = await create_session(
        db, settings.auth, user, provider=provider, user_agent=request.headers.get("user-agent")
    )
    await db.commit()
    set_session_cookie(response, settings.auth, token)
    set_csrf_cookie(response, settings, token)


def clear_cookies(response: Response, settings: Settings) -> None:
    """Remove the session cookie and issue a CSRF token for "no session"."""
    clear_session_cookie(response, settings.auth)
    set_csrf_cookie(response, settings, None)


async def end_session(
    db: AsyncSession, settings: Settings, request: Request, response: Response
) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        user_id = await revoke_token(db, token)
        if user_id is not None:
            await audit.record(db, audit.Actor.user(user_id), audit.AuditAction.LOGOUT)
        await db.commit()
    clear_cookies(response, settings)
