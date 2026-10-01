"""Creating and looking up users."""

from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import LOCAL_PROVIDER, Identity
from app.core.config import AuthSettings
from app.core.errors import ProblemError
from app.users.models import User, UserRole


def check_password_policy(settings: AuthSettings, password: str) -> None:
    """Raise 422 if the password is too short. Length is the only rule (NIST SP 800-63B)."""
    if len(password) < settings.password_min_length:
        raise ProblemError(
            422,
            detail="The password is too short.",
            type="urn:ollamail:problem:password-too-short",
            min_length=settings.password_min_length,
        )


async def any_user_exists(db: AsyncSession) -> bool:
    return bool(await db.scalar(select(exists().where(User.id.is_not(None)))))


async def get_user_by_email(db: AsyncSession, email: str) -> User | None:
    return await db.scalar(select(User).where(User.email == email))


async def add_local_user(
    db: AsyncSession,
    *,
    email: str,
    display_name: str,
    password_hash: str,
    role: UserRole,
    language: str = "en",
    timezone: str = "UTC",
) -> User:
    """Insert a user with a local identity (caller commits). 409 if the e-mail is taken.

    ``email`` must be normalised (``app.users.schemas.normalize_email``).
    """
    if await get_user_by_email(db, email) is not None:
        raise _email_taken()
    user = User(
        email=email,
        display_name=display_name,
        role=role,
        language=language,
        timezone=timezone,
        is_active=True,
    )
    db.add(user)
    try:
        await db.flush()
        db.add(
            Identity(
                user_id=user.id,
                provider=LOCAL_PROVIDER,
                subject=str(user.id),
                password_hash=password_hash,
            )
        )
        await db.flush()
        await db.refresh(user)
    except IntegrityError:
        # A concurrent request created the same e-mail address.
        await db.rollback()
        raise _email_taken() from None
    return user


def _email_taken() -> ProblemError:
    return ProblemError(
        409,
        detail="A user with this e-mail address already exists.",
        type="urn:ollamail:problem:email-taken",
    )
