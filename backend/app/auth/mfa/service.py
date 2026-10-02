"""Second factors of local accounts: status, enrolment, verification, enforcement."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import rate_limit
from app.auth.keys import derive_key, keyed_digest
from app.auth.mfa import recovery, totp
from app.auth.mfa.models import Passkey, PendingLogin, RecoveryCode, TotpFactor
from app.auth.mfa.pending import ENROLL, MAX_ATTEMPTS, VERIFY
from app.auth.models import LOCAL_PROVIDER, Identity, MfaEnforcement
from app.auth.policy import get_policy
from app.core.config import Settings
from app.core.errors import ProblemError
from app.users.models import User, UserRole

TOTP = "totp"
WEBAUTHN = "webauthn"
RECOVERY = "recovery"


def too_many(retry_after: int) -> ProblemError:
    return ProblemError(
        429,
        detail="Too many attempts. Try again later.",
        type="urn:ollamail:problem:too-many-attempts",
        retry_after=retry_after,
    )


def invalid_code() -> ProblemError:
    return ProblemError(
        401, detail="The code is not valid.", type="urn:ollamail:problem:mfa-invalid"
    )


def pending_expired() -> ProblemError:
    return ProblemError(
        401,
        detail="The sign-in has expired. Enter your password again.",
        type="urn:ollamail:problem:mfa-expired",
    )


def mfa_required() -> ProblemError:
    return ProblemError(
        409,
        detail="Two-factor authentication is required for this account.",
        type="urn:ollamail:problem:mfa-required",
    )


def mfa_unavailable() -> ProblemError:
    return ProblemError(
        409,
        detail="Two-factor authentication is only available for local accounts.",
        type="urn:ollamail:problem:mfa-unavailable",
    )


@dataclass(frozen=True)
class Factors:
    totp: bool
    passkeys: list[Passkey]
    recovery_remaining: int

    @property
    def any(self) -> bool:
        return self.totp or bool(self.passkeys)


async def has_local_password(db: AsyncSession, user_id: uuid.UUID) -> bool:
    found = await db.scalar(
        select(Identity.id).where(
            Identity.user_id == user_id,
            Identity.provider == LOCAL_PROVIDER,
            Identity.password_hash.is_not(None),
        )
    )
    return found is not None


async def factors(db: AsyncSession, user_id: uuid.UUID) -> Factors:
    confirmed = await db.scalar(
        select(TotpFactor.id).where(
            TotpFactor.user_id == user_id, TotpFactor.confirmed_at.is_not(None)
        )
    )
    passkeys = list(
        await db.scalars(
            select(Passkey).where(Passkey.user_id == user_id).order_by(Passkey.created_at)
        )
    )
    remaining = await db.scalar(
        select(func.count())
        .select_from(RecoveryCode)
        .where(RecoveryCode.user_id == user_id, RecoveryCode.used_at.is_(None))
    )
    return Factors(totp=confirmed is not None, passkeys=passkeys, recovery_remaining=remaining or 0)


def applies(enforcement: MfaEnforcement, role: UserRole) -> bool:
    if enforcement is MfaEnforcement.ALL:
        return True
    return enforcement is MfaEnforcement.ADMINS and role is UserRole.ADMIN


async def enforced_for(db: AsyncSession, user: User) -> bool:
    return applies((await get_policy(db)).mfa_enforcement, user.role)


async def login_step(db: AsyncSession, user: User) -> str | None:
    """After a correct password: ``verify`` (second factor needed), ``enroll`` (2FA is
    enforced and the user has none yet) or ``None`` (sign in now)."""
    if (await factors(db, user.id)).any:
        return VERIFY
    if await enforced_for(db, user):
        return ENROLL
    return None


def verify_methods(found: Factors, passkeys_configured: bool) -> list[str]:
    methods = []
    if found.passkeys and passkeys_configured:
        methods.append(WEBAUTHN)
    if found.totp:
        methods.append(TOTP)
    if found.recovery_remaining:
        methods.append(RECOVERY)
    return methods


def enroll_methods(passkeys_configured: bool) -> list[str]:
    return [WEBAUTHN, TOTP] if passkeys_configured else [TOTP]


# -- Throttling --------------------------------------------------------------------------


def _account_key(settings: Settings, user_id: uuid.UUID) -> str:
    key = derive_key(settings.security, "rate-limit")
    return "mfa:" + keyed_digest(key, str(user_id))


async def throttle(db: AsyncSession, settings: Settings, user_id: uuid.UUID) -> None:
    """Count a second-factor attempt for the account (committed at once); 429 above
    ``OLLAMAIL_AUTH_LOGIN_MAX_ATTEMPTS`` per window, like the password lockout."""
    window = timedelta(minutes=settings.auth.login_window_minutes)
    hit = await rate_limit.hit(db, _account_key(settings, user_id), window)
    await db.commit()
    if hit.count > settings.auth.login_max_attempts:
        raise too_many(hit.retry_after())


async def reset_throttle(db: AsyncSession, settings: Settings, user_id: uuid.UUID) -> None:
    await rate_limit.reset(db, _account_key(settings, user_id))


async def count_failure(db: AsyncSession, pending: PendingLogin) -> bool:
    """Count a wrong code on the pending login; ``True`` if it was dropped (limit)."""
    pending.attempts += 1
    if pending.attempts >= MAX_ATTEMPTS:
        await db.delete(pending)
        return True
    return False


# -- TOTP --------------------------------------------------------------------------------


async def begin_totp(db: AsyncSession, user_id: uuid.UUID) -> str:
    """A new unconfirmed secret (replaces an unconfirmed one); 409 if TOTP is active."""
    factor = await db.scalar(
        select(TotpFactor).where(TotpFactor.user_id == user_id).with_for_update()
    )
    if factor is not None and factor.confirmed_at is not None:
        raise ProblemError(
            409,
            detail="An authenticator app is already set up.",
            type="urn:ollamail:problem:totp-exists",
        )
    secret = totp.new_secret()
    if factor is None:
        db.add(TotpFactor(user_id=user_id, secret=secret))
    else:
        factor.secret = secret
        factor.last_used_step = None
    await db.flush()
    return secret


async def confirm_totp(db: AsyncSession, user_id: uuid.UUID, code: str) -> bool:
    """Activate the secret from ``begin_totp`` if ``code`` matches it."""
    factor = await db.scalar(
        select(TotpFactor)
        .where(TotpFactor.user_id == user_id, TotpFactor.confirmed_at.is_(None))
        .with_for_update()
    )
    if factor is None:
        raise ProblemError(
            409,
            detail="Start the setup of the authenticator app first.",
            type="urn:ollamail:problem:totp-not-started",
        )
    now = datetime.now(UTC)
    step = totp.matching_step(factor.secret, code, now)
    if step is None:
        return False
    factor.confirmed_at = now
    factor.last_used_step = step
    return True


async def check_totp(db: AsyncSession, user_id: uuid.UUID, code: str) -> bool:
    """Verify a code of the active TOTP; each time step is accepted once (no replay)."""
    factor = await db.scalar(
        select(TotpFactor)
        .where(TotpFactor.user_id == user_id, TotpFactor.confirmed_at.is_not(None))
        .with_for_update()
    )
    if factor is None:
        return False
    step = totp.matching_step(factor.secret, code, datetime.now(UTC))
    if step is None or (factor.last_used_step is not None and step <= factor.last_used_step):
        return False
    factor.last_used_step = step
    return True


async def remove_totp(db: AsyncSession, user_id: uuid.UUID) -> bool:
    result = await db.execute(
        delete(TotpFactor).where(TotpFactor.user_id == user_id).returning(TotpFactor.confirmed_at)
    )
    return any(confirmed is not None for (confirmed,) in result.all())


# -- Recovery codes ----------------------------------------------------------------------


async def replace_recovery_codes(
    db: AsyncSession, settings: Settings, user_id: uuid.UUID
) -> list[str]:
    """New codes; all previous ones stop working."""
    codes = recovery.generate()
    await db.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user_id))
    db.add_all(
        RecoveryCode(user_id=user_id, code_hash=recovery.code_hash(settings.security, code))
        for code in codes
    )
    await db.flush()
    return codes


async def ensure_recovery_codes(
    db: AsyncSession, settings: Settings, user_id: uuid.UUID
) -> list[str] | None:
    """Codes for the first factor of an account; ``None`` if unused codes exist."""
    if (await factors(db, user_id)).recovery_remaining:
        return None
    return await replace_recovery_codes(db, settings, user_id)


async def use_recovery_code(
    db: AsyncSession, settings: Settings, user_id: uuid.UUID, code: str
) -> bool:
    if not recovery.looks_like_code(code):
        return False
    result = await db.execute(
        update(RecoveryCode)
        .where(
            RecoveryCode.user_id == user_id,
            RecoveryCode.code_hash == recovery.code_hash(settings.security, code),
            RecoveryCode.used_at.is_(None),
        )
        .values(used_at=datetime.now(UTC))
        .returning(RecoveryCode.id)
    )
    return result.first() is not None


# -- Removal -----------------------------------------------------------------------------


async def drop_codes_without_factor(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Recovery codes only make sense next to a factor."""
    if not (await factors(db, user_id)).any:
        await db.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user_id))


async def check_removal_allowed(
    db: AsyncSession, user: User, *, totp: bool = False, passkey_id: uuid.UUID | None = None
) -> None:
    """409 if removing the TOTP or the passkey ``passkey_id`` would leave the account
    without a factor while 2FA is enforced for it. Call before removing."""
    if not await enforced_for(db, user):
        return
    found = await factors(db, user.id)
    keeps_totp = found.totp and not totp
    keeps_passkey = any(passkey.id != passkey_id for passkey in found.passkeys)
    if not keeps_totp and not keeps_passkey:
        raise mfa_required()


async def reset_all(db: AsyncSession, user_id: uuid.UUID) -> dict[str, int]:
    """Remove every factor and recovery code (emergency access, CLI)."""
    counts = {}
    for name, model in (
        ("totp", TotpFactor),
        ("passkeys", Passkey),
        ("recovery_codes", RecoveryCode),
        ("pending", PendingLogin),
    ):
        result = await db.execute(delete(model).where(model.user_id == user_id).returning(model.id))
        counts[name] = len(result.all())
    return counts
