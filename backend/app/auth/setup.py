"""Bootstrap: the first account of a fresh instance becomes admin.

``POST /api/setup`` works only while no user exists and requires the setup token, so a
freshly exposed instance cannot be claimed by whoever finds it first. The token is
``OLLAMAIL_SETUP_TOKEN`` or, if unset, derived from ``OLLAMAIL_SECRET_KEY`` (identical on
all API instances, nothing stored). It is logged at start-up while setup is pending and
printed by ``python -m app.cli setup-token``. Once an admin exists it is useless.

Two concurrent setup requests are serialised by a transaction-level advisory lock; the
second one sees the first admin and gets 409.
"""

import base64
import hmac

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.keys import derive_key
from app.core.config import Settings
from app.core.db import Database
from app.core.errors import ProblemError
from app.core.logging import get_logger
from app.users.models import User, UserRole
from app.users.service import add_local_user, any_user_exists

log = get_logger(__name__)

# pg_advisory_xact_lock key for the bootstrap (arbitrary, constant; "ollamail" in ASCII).
SETUP_LOCK_KEY = 0x6F6C6C616D61696C


def setup_token(settings: Settings) -> str:
    configured = settings.security.setup_token
    if configured is not None and configured.get_secret_value():
        return configured.get_secret_value()
    key = derive_key(settings.security, "setup-token")
    return base64.b32encode(key[:15]).decode()


def setup_token_valid(settings: Settings, candidate: str) -> bool:
    expected = setup_token(settings)
    return hmac.compare_digest(candidate.strip().encode(), expected.encode())


def already_initialized() -> ProblemError:
    return ProblemError(
        409,
        detail="The instance is already set up.",
        type="urn:ollamail:problem:already-initialized",
    )


async def create_first_admin(
    db: AsyncSession,
    *,
    email: str,
    display_name: str,
    password_hash: str,
    language: str,
    timezone: str,
) -> User:
    """Create the first admin; 409 if any user exists. Caller commits (releases the lock)."""
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": SETUP_LOCK_KEY})
    if await any_user_exists(db):
        raise already_initialized()
    return await add_local_user(
        db,
        email=email,
        display_name=display_name,
        password_hash=password_hash,
        role=UserRole.ADMIN,
        language=language,
        timezone=timezone,
    )


async def log_setup_status(database: Database, settings: Settings) -> None:
    """At start-up: if setup is pending, log how to complete it (incl. the derived token)."""
    try:
        async with database.sessionmaker() as db:
            initialized = await any_user_exists(db)
    except Exception as exc:
        # Database down or not migrated yet; /api/setup/status reports it later.
        log.warning("setup_status_unknown", error_type=type(exc).__name__)
        return
    if initialized:
        return
    if settings.security.setup_token is not None:
        log.warning("setup_pending", hint="Use OLLAMAIL_SETUP_TOKEN to create the first admin.")
    else:
        # Logged on purpose (one-time bootstrap secret, useless once an admin exists).
        # The field is not called "*_token" because the log filter would drop it.
        log.warning(
            "setup_pending",
            hint="Use this setup code to create the first admin.",
            setup_code=setup_token(settings),
        )
