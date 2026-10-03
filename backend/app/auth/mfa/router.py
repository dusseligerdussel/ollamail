"""Endpoints for second factors (Account → Security) and the login steps after the password.

Login (``POST /auth/login`` answers 202 with ``MfaChallenge`` instead of signing in):

* ``mfa_required``: ``POST /auth/mfa/verify`` (TOTP or recovery code) or
  ``POST /auth/mfa/verify/passkey/options`` + ``POST /auth/mfa/verify/passkey``.
* ``mfa_enrollment_required``: set up TOTP or a passkey with the endpoints below; confirming
  the first factor signs in (``MfaEnrolled.user``).

Passwordless: ``POST /auth/passkey/options`` + ``POST /auth/passkey/login``.
"""

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.auth import service as auth_service
from app.auth.dependencies import SettingsDep
from app.auth.mfa import pending as pending_store
from app.auth.mfa import service
from app.auth.mfa.models import Passkey, PendingLogin
from app.auth.mfa.passkeys import (
    PasskeyError,
    RelyingParty,
    StoredCredential,
    authentication_options,
    credential_id_of,
    registration_options,
    relying_party,
    verify_authentication,
    verify_registration,
)
from app.auth.mfa.schemas import (
    CodeRequest,
    MfaChallenge,
    MfaEnrolled,
    MfaStatus,
    MfaVerifyRequest,
    PasskeyAssertion,
    PasskeyEnrolled,
    PasskeyRead,
    PasskeyRegistration,
    RecoveryCodes,
    TotpSetup,
)
from app.auth.mfa.totp import provisioning_uri, qr_svg
from app.auth.models import LOCAL_PROVIDER
from app.auth.policy import local_login_enabled
from app.auth.reauth import REAUTH_RESPONSES, RecentAuthDep
from app.auth.sessions import SESSION_COOKIE, resolve_session, revoke_token
from app.core.config import Settings
from app.core.db import get_db
from app.core.errors import ProblemError
from app.core.logging import get_logger
from app.users.models import User
from app.users.schemas import UserRead

log = get_logger(__name__)

DbDep = Annotated[AsyncSession, Depends(get_db)]

router = APIRouter(prefix="/auth", tags=["auth"])

_UNAUTHORIZED: dict[int | str, dict[str, Any]] = {401: {"description": "Not signed in"}}
_STEP: dict[int | str, dict[str, Any]] = {
    401: {"description": "Wrong code, or the pending sign-in expired (mfa-expired)"},
    429: {"description": "Too many attempts (rate limit or account lockout)"},
}
_NOT_CONFIGURED: dict[int | str, dict[str, Any]] = {
    409: {"description": "Passkeys are not configured, or 2FA not available for this account"}
}


# -- Who is setting up a factor ----------------------------------------------------------


@dataclass
class Subject:
    """A signed-in user, or a user in the middle of a login that must enrol a factor."""

    user: User
    session_id: uuid.UUID | None
    pending: PendingLogin | None


async def _subject(request: Request, db: DbDep, settings: SettingsDep) -> Subject:
    token = request.cookies.get(SESSION_COOKIE)
    current = await resolve_session(db, settings.auth, token) if token else None
    if current is not None:
        user = await db.get(User, current.user_id)
        if user is not None:
            return await _local_only(db, Subject(user, current.session_id, None))
    pending = await pending_store.current(db, request, {pending_store.ENROLL})
    if pending is not None and pending.user_id is not None:
        user = await db.get(User, pending.user_id)
        if user is not None and user.is_active:
            return await _local_only(db, Subject(user, None, pending))
    raise ProblemError(401, detail="Authentication required.")


async def _local_only(db: AsyncSession, subject: Subject) -> Subject:
    if not await service.has_local_password(db, subject.user.id):
        raise service.mfa_unavailable()
    return subject


async def _signed_in(request: Request, db: DbDep, settings: SettingsDep) -> Subject:
    subject = await _subject(request, db, settings)
    if subject.session_id is None:
        raise ProblemError(401, detail="Authentication required.")
    return subject


SubjectDep = Annotated[Subject, Depends(_subject)]
SignedInDep = Annotated[Subject, Depends(_signed_in)]


def _rp(settings: Settings) -> RelyingParty:
    rp = relying_party(settings.auth)
    if rp is None:
        raise ProblemError(
            409,
            detail="Passkeys are not configured on this instance.",
            type="urn:ollamail:problem:passkeys-not-configured",
        )
    return rp


def _passkey_read(passkey: Passkey) -> PasskeyRead:
    return PasskeyRead(
        id=passkey.id,
        name=passkey.name,
        created_at=passkey.created_at,
        last_used_at=passkey.last_used_at,
        backed_up=passkey.backed_up,
    )


def _stored(passkeys: list[Passkey]) -> list[StoredCredential]:
    return [StoredCredential(p.credential_id, list(p.transports)) for p in passkeys]


# -- Login steps -------------------------------------------------------------------------


async def second_step(
    db: AsyncSession, settings: Settings, request: Request, user: User, step: str
) -> JSONResponse:
    """The 202 answer of ``POST /auth/login`` for an account that needs a second step."""
    configured = relying_party(settings.auth) is not None
    if step == pending_store.VERIFY:
        methods = service.verify_methods(await service.factors(db, user.id), configured)
        state = "mfa_required"
    else:
        methods = service.enroll_methods(configured)
        state = "mfa_enrollment_required"
    cookie_carrier = Response()
    # A session of someone else in this browser (e.g. the admin who sent an invitation) ends
    # here: otherwise the set-up endpoints would act on that account instead.
    previous = request.cookies.get(SESSION_COOKIE)
    if previous:
        await revoke_token(db, previous)
        auth_service.clear_cookies(cookie_carrier, settings)
    pending = await pending_store.start(
        db, settings.auth, request, cookie_carrier, purpose=step, user_id=user.id
    )
    await db.commit()
    body = MfaChallenge(status=state, methods=methods, expires_at=pending.expires_at)
    response = JSONResponse(body.model_dump(mode="json"), status_code=status.HTTP_202_ACCEPTED)
    for value in cookie_carrier.headers.getlist("set-cookie"):
        response.headers.append("set-cookie", value)
    log.info("login_second_step", user_id=user.id, step=step)
    return response


async def _complete_login(
    db: AsyncSession,
    settings: Settings,
    request: Request,
    response: Response,
    user: User,
    pending: PendingLogin | None,
    details: dict[str, Any],
) -> UserRead:
    if pending is not None:
        await db.delete(pending)
    await service.reset_throttle(db, settings, user.id)
    await audit.record(
        db,
        audit.Actor.user(user.id),
        audit.AuditAction.LOGIN_SUCCEEDED,
        details={"provider": LOCAL_PROVIDER, **details},
    )
    pending_store.clear_cookie(response, settings.auth)
    await auth_service.start_session(db, settings, request, response, user, provider=LOCAL_PROVIDER)
    log.info("login_succeeded", user_id=user.id, provider=LOCAL_PROVIDER, mfa=details["mfa"])
    return UserRead.model_validate(user)


async def _failed(db: AsyncSession, user_id: uuid.UUID, method: str, reason: str) -> None:
    log.info("login_failed", reason=reason, mfa=method)
    await audit.record(
        db,
        audit.ANONYMOUS,
        audit.AuditAction.LOGIN_FAILED,
        audit.Target.of(audit.TargetType.USER, user_id),
        {"provider": LOCAL_PROVIDER, "reason": reason, "mfa": method},
    )


async def _verify_state(
    db: AsyncSession, settings: Settings, request: Request, method: str
) -> tuple[PendingLogin, User]:
    """The locked pending login after IP and account throttling; 401 if it is gone."""
    await auth_service.throttle_ip(db, settings, request)
    pending = await pending_store.current(db, request, {pending_store.VERIFY}, lock=False)
    user = await db.get(User, pending.user_id) if pending and pending.user_id else None
    if pending is None or user is None or not user.is_active:
        raise service.pending_expired()
    try:
        await service.throttle(db, settings, user.id)
    except ProblemError:
        await _failed(db, user.id, method, "locked")
        await db.commit()
        raise
    # Re-read with a row lock: parallel attempts are counted one after another.
    locked = await pending_store.current(db, request, {pending_store.VERIFY})
    if locked is None:
        raise service.pending_expired()
    return locked, user


async def _reject(
    db: AsyncSession, settings: Settings, pending: PendingLogin, user: User, method: str
) -> ProblemError:
    dropped = await service.count_failure(db, pending)
    await _failed(db, user.id, method, "invalid_code")
    await db.commit()
    return service.pending_expired() if dropped else service.invalid_code()


@router.post("/mfa/verify", responses=_STEP)
async def verify(
    body: MfaVerifyRequest,
    request: Request,
    response: Response,
    db: DbDep,
    settings: SettingsDep,
) -> UserRead:
    """Second step of the login with a TOTP code or a recovery code."""
    pending, user = await _verify_state(db, settings, request, body.method)
    if body.method == "totp":
        valid = await service.check_totp(db, user.id, body.code)
    else:
        valid = await service.use_recovery_code(db, settings, user.id, body.code)
    if not valid:
        raise await _reject(db, settings, pending, user, body.method)
    details: dict[str, Any] = {"mfa": body.method}
    if body.method == "recovery":
        details["recovery_codes_remaining"] = (
            await service.factors(db, user.id)
        ).recovery_remaining
    return await _complete_login(db, settings, request, response, user, pending, details)


@router.post("/mfa/verify/passkey/options", responses={**_STEP, **_NOT_CONFIGURED})
async def verify_passkey_options(
    request: Request, db: DbDep, settings: SettingsDep
) -> dict[str, Any]:
    """WebAuthn request options for the second step (``navigator.credentials.get``)."""
    rp = _rp(settings)
    pending = await pending_store.current(db, request, {pending_store.VERIFY})
    if pending is None or pending.user_id is None:
        raise service.pending_expired()
    passkeys = (await service.factors(db, pending.user_id)).passkeys
    challenge = secrets.token_bytes(32)
    pending.webauthn_challenge = challenge
    await db.commit()
    return authentication_options(
        rp, challenge=challenge, allowed=_stored(passkeys), passwordless=False
    )


@router.post("/mfa/verify/passkey", responses={**_STEP, **_NOT_CONFIGURED})
async def verify_passkey(
    body: PasskeyAssertion,
    request: Request,
    response: Response,
    db: DbDep,
    settings: SettingsDep,
) -> UserRead:
    """Second step of the login with a passkey."""
    rp = _rp(settings)
    pending, user = await _verify_state(db, settings, request, service.WEBAUTHN)
    challenge, pending.webauthn_challenge = pending.webauthn_challenge, None
    credential_id = credential_id_of(body.credential)
    passkey = (
        await db.scalar(
            select(Passkey).where(
                Passkey.user_id == user.id, Passkey.credential_id == credential_id
            )
        )
        if credential_id and challenge
        else None
    )
    if passkey is None or challenge is None:
        raise await _reject(db, settings, pending, user, service.WEBAUTHN)
    try:
        sign_count = verify_authentication(
            rp,
            body.credential,
            challenge,
            public_key=passkey.public_key,
            sign_count=passkey.sign_count,
            passwordless=False,
        )
    except PasskeyError:
        raise await _reject(db, settings, pending, user, service.WEBAUTHN) from None
    passkey.sign_count = sign_count
    passkey.last_used_at = datetime.now(UTC)
    details = {"mfa": service.WEBAUTHN}
    return await _complete_login(db, settings, request, response, user, pending, details)


@router.post("/mfa/cancel", status_code=status.HTTP_204_NO_CONTENT)
async def cancel(request: Request, db: DbDep, settings: SettingsDep) -> Response:
    """Abort a pending login (back to the password)."""
    await pending_store.discard(db, request)
    await db.commit()
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    pending_store.clear_cookie(response, settings.auth)
    return response


# -- Passwordless ------------------------------------------------------------------------


def _local_login_disabled() -> ProblemError:
    return ProblemError(
        403,
        detail="Sign-in with local accounts is disabled.",
        type="urn:ollamail:problem:local-login-disabled",
    )


@router.post(
    "/passkey/options",
    responses={403: {"description": "Local login is disabled"}, **_NOT_CONFIGURED, **_STEP},
)
async def passkey_login_options(
    request: Request, response: Response, db: DbDep, settings: SettingsDep
) -> dict[str, Any]:
    """WebAuthn request options for signing in with a passkey instead of a password."""
    if not await local_login_enabled(db):
        raise _local_login_disabled()
    rp = _rp(settings)
    await auth_service.throttle_ip(db, settings, request)
    challenge = secrets.token_bytes(32)
    await pending_store.start(
        db,
        settings.auth,
        request,
        response,
        purpose=pending_store.PASSKEY,
        user_id=None,
        webauthn_challenge=challenge,
    )
    await db.commit()
    return authentication_options(rp, challenge=challenge, allowed=[], passwordless=True)


@router.post(
    "/passkey/login",
    responses={403: {"description": "Local login is disabled"}, **_NOT_CONFIGURED, **_STEP},
)
async def passkey_login(
    body: PasskeyAssertion,
    request: Request,
    response: Response,
    db: DbDep,
    settings: SettingsDep,
) -> UserRead:
    """Sign in with a passkey (counts as two factors: possession and user verification)."""
    if not await local_login_enabled(db):
        raise _local_login_disabled()
    rp = _rp(settings)
    await auth_service.throttle_ip(db, settings, request)
    pending = await pending_store.current(db, request, {pending_store.PASSKEY})
    if pending is None or pending.webauthn_challenge is None:
        raise service.pending_expired()
    challenge = pending.webauthn_challenge
    # Single use, whatever the outcome.
    await db.delete(pending)
    credential_id = credential_id_of(body.credential)
    passkey = (
        await db.scalar(select(Passkey).where(Passkey.credential_id == credential_id))
        if credential_id
        else None
    )
    user = await db.get(User, passkey.user_id) if passkey else None
    valid = (
        passkey is not None
        and user is not None
        and user.is_active
        and await service.has_local_password(db, user.id)
    )
    if valid and passkey is not None:
        try:
            sign_count = verify_authentication(
                rp,
                body.credential,
                challenge,
                public_key=passkey.public_key,
                sign_count=passkey.sign_count,
                passwordless=True,
            )
        except PasskeyError:
            valid = False
    if not valid or passkey is None or user is None:
        log.info("login_failed", reason="invalid_passkey")
        await audit.record(
            db,
            audit.ANONYMOUS,
            audit.AuditAction.LOGIN_FAILED,
            None,
            {"provider": LOCAL_PROVIDER, "reason": "invalid_passkey", "mfa": "webauthn"},
        )
        await db.commit()
        raise ProblemError(
            401,
            detail="This passkey cannot be used to sign in.",
            type="urn:ollamail:problem:passkey-invalid",
        )
    passkey.sign_count = sign_count
    passkey.last_used_at = datetime.now(UTC)
    details = {"mfa": service.WEBAUTHN, "passwordless": True}
    return await _complete_login(db, settings, request, response, user, None, details)


# -- Account → Security ------------------------------------------------------------------


@router.get("/mfa", responses=_UNAUTHORIZED)
async def get_status(request: Request, db: DbDep, settings: SettingsDep) -> MfaStatus:
    """Second factors of the own account."""
    token = request.cookies.get(SESSION_COOKIE)
    current = await resolve_session(db, settings.auth, token) if token else None
    user = await db.get(User, current.user_id) if current else None
    if user is None:
        raise ProblemError(401, detail="Authentication required.")
    found = await service.factors(db, user.id)
    return MfaStatus(
        available=await service.has_local_password(db, user.id),
        enforced=await service.enforced_for(db, user),
        passkeys_configured=relying_party(settings.auth) is not None,
        totp=found.totp,
        passkeys=[_passkey_read(p) for p in found.passkeys],
        recovery_codes_remaining=found.recovery_remaining,
    )


async def _enrolled(
    db: AsyncSession,
    settings: Settings,
    request: Request,
    response: Response,
    subject: Subject,
    method: str,
    details: dict[str, Any],
) -> MfaEnrolled:
    """Shared tail of TOTP confirmation and passkey registration."""
    codes = await service.ensure_recovery_codes(db, settings, subject.user.id)
    actor = audit.Actor.user(subject.user.id)
    target = audit.Target.of(audit.TargetType.USER, subject.user.id)
    await audit.record(
        db, actor, audit.AuditAction.MFA_ENABLED, target, {"method": method, **details}
    )
    if codes:
        await audit.record(
            db, actor, audit.AuditAction.MFA_RECOVERY_CODES_GENERATED, target, {"count": len(codes)}
        )
    if subject.pending is None:
        await db.commit()
        log.info("mfa_enabled", user_id=subject.user.id, method=method)
        return MfaEnrolled(recovery_codes=codes)
    log.info("mfa_enabled", user_id=subject.user.id, method=method, enrolment=True)
    user = await _complete_login(
        db, settings, request, response, subject.user, subject.pending, {"mfa": method}
    )
    return MfaEnrolled(recovery_codes=codes, user=user)


@router.post("/mfa/totp/setup", responses={**_UNAUTHORIZED, 409: {"description": "Active"}})
async def setup_totp(subject: SubjectDep, db: DbDep) -> TotpSetup:
    """Start setting up an authenticator app: a new secret and its QR code."""
    secret = await service.begin_totp(db, subject.user.id)
    await db.commit()
    uri = provisioning_uri(secret, subject.user.email)
    return TotpSetup(secret=secret, uri=uri, qr_svg=qr_svg(uri))


@router.post(
    "/mfa/totp/confirm",
    responses={
        **_UNAUTHORIZED,
        400: {"description": "Wrong code (mfa-invalid)"},
        429: _STEP[429],
    },
)
async def confirm_totp(
    body: CodeRequest,
    subject: SubjectDep,
    request: Request,
    response: Response,
    db: DbDep,
    settings: SettingsDep,
) -> MfaEnrolled:
    """Activate the authenticator app with a current code. The first factor of an account
    comes with recovery codes; during an enforced enrolment this also signs in."""
    await service.throttle(db, settings, subject.user.id)
    if not await service.confirm_totp(db, subject.user.id, body.code):
        if subject.pending is not None and await service.count_failure(db, subject.pending):
            await db.commit()
            raise service.pending_expired()
        await db.commit()
        raise service.invalid_code(status.HTTP_400_BAD_REQUEST)
    await service.reset_throttle(db, settings, subject.user.id)
    return await _enrolled(db, settings, request, response, subject, service.TOTP, {})


@router.delete(
    "/mfa/totp",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        **_UNAUTHORIZED,
        **REAUTH_RESPONSES,
        404: {"description": "Not set up"},
        409: {"description": "2FA enforced"},
    },
)
async def remove_totp(subject: SignedInDep, _: RecentAuthDep, db: DbDep) -> Response:
    """Remove the authenticator app. Needs a recent confirmation (app/auth/reauth.py)."""
    await service.check_removal_allowed(db, subject.user, totp=True)
    if not await service.remove_totp(db, subject.user.id):
        raise ProblemError(404, detail="No authenticator app is set up.")
    await _after_removal(db, subject.user, {"method": service.TOTP})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _after_removal(db: AsyncSession, user: User, details: dict[str, Any]) -> None:
    await service.drop_codes_without_factor(db, user.id)
    await audit.record(
        db,
        audit.Actor.user(user.id),
        audit.AuditAction.MFA_DISABLED,
        audit.Target.of(audit.TargetType.USER, user.id),
        {**details, "via": "self"},
    )
    await db.commit()
    log.info("mfa_disabled", user_id=user.id, method=details["method"])


@router.post("/mfa/passkeys/options", responses={**_UNAUTHORIZED, **_NOT_CONFIGURED})
async def passkey_registration_options(
    subject: SubjectDep, request: Request, response: Response, db: DbDep, settings: SettingsDep
) -> dict[str, Any]:
    """WebAuthn creation options for a new passkey (``navigator.credentials.create``)."""
    rp = _rp(settings)
    challenge = secrets.token_bytes(32)
    if subject.pending is not None:
        subject.pending.webauthn_challenge = challenge
    else:
        await pending_store.start(
            db,
            settings.auth,
            request,
            response,
            purpose=pending_store.REGISTER,
            user_id=subject.user.id,
            session_id=subject.session_id,
            webauthn_challenge=challenge,
        )
    existing = (await service.factors(db, subject.user.id)).passkeys
    await db.commit()
    return registration_options(
        rp,
        user_id=subject.user.id,
        user_name=subject.user.email,
        display_name=subject.user.display_name,
        challenge=challenge,
        existing=_stored(existing),
    )


@router.post(
    "/mfa/passkeys",
    status_code=status.HTTP_201_CREATED,
    responses={
        **_UNAUTHORIZED,
        **_NOT_CONFIGURED,
        400: {"description": "The passkey could not be verified"},
    },
)
async def register_passkey(
    body: PasskeyRegistration,
    subject: SubjectDep,
    request: Request,
    response: Response,
    db: DbDep,
    settings: SettingsDep,
) -> PasskeyEnrolled:
    """Store a new passkey after verifying the browser's response."""
    rp = _rp(settings)
    state = subject.pending
    if state is None:
        state = await pending_store.current(db, request, {pending_store.REGISTER})
        if state is not None and state.session_id != subject.session_id:
            state = None
    challenge = state.webauthn_challenge if state else None
    invalid = ProblemError(
        400,
        detail="The passkey could not be verified.",
        type="urn:ollamail:problem:passkey-invalid",
    )
    if state is None or challenge is None:
        # Signed in: a stale or reused challenge is a failed registration, not a lost session.
        raise service.pending_expired() if subject.pending is not None else invalid
    state.webauthn_challenge = None
    try:
        new = verify_registration(rp, body.credential, challenge)
    except PasskeyError:
        await db.commit()
        raise invalid from None
    if await db.scalar(select(Passkey.id).where(Passkey.credential_id == new.credential_id)):
        raise ProblemError(
            409,
            detail="This passkey is already registered.",
            type="urn:ollamail:problem:passkey-exists",
        )
    passkey = Passkey(
        user_id=subject.user.id,
        credential_id=new.credential_id,
        public_key=new.public_key,
        sign_count=new.sign_count,
        transports=new.transports,
        name=body.name,
        backed_up=new.backed_up,
    )
    db.add(passkey)
    if subject.pending is None:
        await db.delete(state)
        pending_store.clear_cookie(response, settings.auth)
    await db.flush()
    await db.refresh(passkey)
    enrolled = await _enrolled(
        db, settings, request, response, subject, service.WEBAUTHN, {"passkey_id": str(passkey.id)}
    )
    response.status_code = status.HTTP_201_CREATED
    return PasskeyEnrolled(
        recovery_codes=enrolled.recovery_codes, user=enrolled.user, passkey=_passkey_read(passkey)
    )


@router.delete(
    "/mfa/passkeys/{passkey_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        **_UNAUTHORIZED,
        **REAUTH_RESPONSES,
        404: {"description": "No such passkey"},
        409: {"description": "2FA enforced"},
    },
)
async def remove_passkey(
    passkey_id: uuid.UUID, subject: SignedInDep, _: RecentAuthDep, db: DbDep
) -> Response:
    """Remove one of the own passkeys. Needs a recent confirmation."""
    passkey = await db.scalar(
        select(Passkey).where(Passkey.id == passkey_id, Passkey.user_id == subject.user.id)
    )
    if passkey is None:
        raise ProblemError(404, detail="Passkey not found.")
    await service.check_removal_allowed(db, subject.user, passkey_id=passkey.id)
    await db.delete(passkey)
    await db.flush()
    await _after_removal(
        db, subject.user, {"method": service.WEBAUTHN, "passkey_id": str(passkey_id)}
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/mfa/recovery-codes",
    responses={
        **_UNAUTHORIZED,
        **REAUTH_RESPONSES,
        409: {"description": "No second factor set up"},
    },
)
async def regenerate_recovery_codes(
    subject: SignedInDep, _: RecentAuthDep, db: DbDep, settings: SettingsDep
) -> RecoveryCodes:
    """New recovery codes; the previous ones stop working. Needs a recent confirmation."""
    if not (await service.factors(db, subject.user.id)).any:
        raise ProblemError(
            409,
            detail="Set up a passkey or an authenticator app first.",
            type="urn:ollamail:problem:mfa-not-enabled",
        )
    codes = await service.replace_recovery_codes(db, settings, subject.user.id)
    await audit.record(
        db,
        audit.Actor.user(subject.user.id),
        audit.AuditAction.MFA_RECOVERY_CODES_GENERATED,
        audit.Target.of(audit.TargetType.USER, subject.user.id),
        {"count": len(codes)},
    )
    await db.commit()
    return RecoveryCodes(codes=codes)
