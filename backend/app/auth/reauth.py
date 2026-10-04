"""Confirmation before sensitive actions (#144).

Removing a second factor, new recovery codes, the full data export and deleting the own
account need proof that the person in front of the browser is the account owner, not only
a (possibly stolen) session cookie. The session remembers when the user last proved it
(``auth_sessions.authenticated_at``: the sign-in or a confirmation here); sensitive
endpoints depend on ``RecentAuthDep`` and answer 403 ``reauth-required`` when that is
longer ago than ``OLLAMAIL_AUTH_REAUTH_MINUTES``.

Ways to confirm (``GET /auth/reauth`` lists those of the account):

* ``password``: the local password.
* ``totp``: a code of the authenticator app; ``webauthn``: a passkey (local accounts).
* ``sso``: accounts that signed in with a redirect provider (OIDC, GitHub, SAML) sign in
  there again (``login_path``); the new session starts with a fresh ``authenticated_at``.
  The identity provider checks its own credentials and second factor, a stolen ollamail
  cookie does not come with the provider's session.
* ``signin``: always possible; sign out and in again (e.g. LDAP accounts).

Recovery codes are not offered: they are a last resort for a lost factor.

Critical admin actions (#190, #206) depend on ``RecentAdminDep``: deleting a user, changing
a role or deactivating, SCIM tokens and settings, sign-in providers and settings, the role
mapping, AI providers, shared mailbox assignments. Some only need it for part of their
changes and call ``check_recent`` themselves: AI settings when they turn on cloud providers
or assign tasks, creating a shared mailbox together with its first assignments. A stolen
admin cookie alone must not be enough to add an identity provider that links to other
accounts, to point an AI endpoint at a foreign server or to read a shared mailbox.
"""

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.auth import rate_limit
from app.auth.dependencies import AdminSessionDep, CurrentSessionDep, SettingsDep
from app.auth.keys import derive_key, keyed_digest
from app.auth.mfa import pending as pending_store
from app.auth.mfa import service as mfa_service
from app.auth.mfa.models import Passkey
from app.auth.mfa.passkeys import (
    PasskeyError,
    StoredCredential,
    authentication_options,
    credential_id_of,
    relying_party,
    verify_authentication,
)
from app.auth.mfa.schemas import PasskeyAssertion
from app.auth.models import LOCAL_PROVIDER, AuthSession, Identity
from app.auth.passwords import verify_password
from app.auth.providers import AuthProviderRegistry, RedirectAuthProvider
from app.auth.sessions import CurrentSession, mark_authenticated
from app.core.config import Settings
from app.core.db import get_db
from app.core.errors import ProblemError
from app.core.logging import get_logger

log = get_logger(__name__)

DbDep = Annotated[AsyncSession, Depends(get_db)]

REAUTH_REQUIRED = "urn:ollamail:problem:reauth-required"

PASSWORD = "password"
SSO = "sso"
SIGNIN = "signin"

ReauthMethod = Literal["password", "totp", "webauthn", "sso", "signin"]

# For the ``responses`` of sensitive endpoints.
REAUTH_RESPONSES: dict[int | str, dict[str, Any]] = {
    403: {"description": "Confirm the account first (reauth-required, see /auth/reauth)"}
}
# For critical admin actions (``RecentAdminDep``).
ADMIN_REAUTH_RESPONSES: dict[int | str, dict[str, Any]] = {
    403: {"description": "Not an admin, or confirm the account first (reauth-required)"}
}


def reauth_required(settings: Settings) -> ProblemError:
    return ProblemError(
        403,
        detail="Confirm that it is you to continue.",
        type=REAUTH_REQUIRED,
        reauth_minutes=settings.auth.reauth_minutes,
    )


def valid_until(settings: Settings, authenticated_at: datetime | None) -> datetime | None:
    if authenticated_at is None:
        return None
    return authenticated_at + timedelta(minutes=settings.auth.reauth_minutes)


def is_recent(settings: Settings, current: CurrentSession) -> bool:
    until = valid_until(settings, current.authenticated_at)
    return until is not None and until > datetime.now(UTC)


def check_recent(settings: Settings, current: CurrentSession) -> None:
    """403 ``reauth-required`` unless the user confirmed who they are recently; for
    endpoints that need it only for some of their changes."""
    if not is_recent(settings, current):
        raise reauth_required(settings)


async def require_recent_auth(current: CurrentSessionDep, settings: SettingsDep) -> CurrentSession:
    """The session, if the user confirmed who they are recently; 403 otherwise."""
    check_recent(settings, current)
    return current


RecentAuthDep = Annotated[CurrentSession, Depends(require_recent_auth)]


async def require_recent_admin(admin: AdminSessionDep, settings: SettingsDep) -> CurrentSession:
    """An admin session with a recent confirmation (#190); 403 for others, then
    403 ``reauth-required`` when the confirmation is too old."""
    check_recent(settings, admin)
    return admin


RecentAdminDep = Annotated[CurrentSession, Depends(require_recent_admin)]


# -- Schemas -----------------------------------------------------------------------------


class ReauthOptions(BaseModel):
    """How the signed-in user can confirm who they are."""

    # In the order the UI offers them; ``signin`` is always last.
    methods: list[ReauthMethod]
    # ``sso``: the provider's name and its login path below ``/api`` (add ``return_to``).
    provider_display_name: str | None = None
    login_path: str | None = None
    # Minutes a confirmation lasts, and until when the current one is valid (if at all).
    reauth_minutes: int
    valid_until: datetime | None


class ReauthRequest(BaseModel):
    method: Literal["password", "totp"]
    password: str | None = Field(default=None, min_length=1, max_length=1024)
    code: str | None = Field(default=None, min_length=1, max_length=16)

    @model_validator(mode="after")
    def _value_for_method(self) -> "ReauthRequest":
        if (self.method == PASSWORD) != (self.password is not None) or (
            self.method == mfa_service.TOTP
        ) != (self.code is not None):
            raise ValueError("password goes with method 'password', code with 'totp'")
        return self


class ReauthStatus(BaseModel):
    authenticated_at: datetime
    valid_until: datetime


# -- Helpers -----------------------------------------------------------------------------


def _throttle_key(settings: Settings, user_id: uuid.UUID) -> str:
    key = derive_key(settings.security, "rate-limit")
    return "reauth:" + keyed_digest(key, str(user_id))


async def _throttle(db: AsyncSession, settings: Settings, user_id: uuid.UUID) -> None:
    """Count a confirmation attempt (committed at once); 429 above
    ``OLLAMAIL_AUTH_LOGIN_MAX_ATTEMPTS`` per window, like the login lockout."""
    window = timedelta(minutes=settings.auth.login_window_minutes)
    hit = await rate_limit.hit(db, _throttle_key(settings, user_id), window)
    await db.commit()
    if hit.count > settings.auth.login_max_attempts:
        raise mfa_service.too_many(hit.retry_after())


async def _local_password_hash(db: AsyncSession, user_id: uuid.UUID) -> str | None:
    return await db.scalar(
        select(Identity.password_hash).where(
            Identity.user_id == user_id, Identity.provider == LOCAL_PROVIDER
        )
    )


async def _redirect_provider(
    db: AsyncSession, request: Request, current: CurrentSession
) -> RedirectAuthProvider | None:
    """The redirect provider this session was started with, if it is still available."""
    name = await db.scalar(select(AuthSession.provider).where(AuthSession.id == current.session_id))
    if not name or name == LOCAL_PROVIDER:
        return None
    registry: AuthProviderRegistry = request.app.state.auth_providers
    for provider in await registry.available(db):
        if provider.name == name and isinstance(provider, RedirectAuthProvider):
            return provider
    return None


async def _confirmed(
    db: AsyncSession, settings: Settings, current: CurrentSession, method: str
) -> ReauthStatus:
    authenticated_at = await mark_authenticated(db, current.session_id)
    await rate_limit.reset(db, _throttle_key(settings, current.user_id))
    await audit.record(
        db,
        audit.Actor.user(current.user_id),
        audit.AuditAction.REAUTHENTICATED,
        audit.Target.of(audit.TargetType.SESSION, current.session_id),
        {"method": method},
    )
    await db.commit()
    log.info("reauth_succeeded", user_id=current.user_id, method=method)
    until = valid_until(settings, authenticated_at)
    assert until is not None
    return ReauthStatus(authenticated_at=authenticated_at, valid_until=until)


async def _rejected(db: AsyncSession, current: CurrentSession, method: str, reason: str) -> None:
    await audit.record(
        db,
        audit.Actor.user(current.user_id),
        audit.AuditAction.REAUTH_FAILED,
        audit.Target.of(audit.TargetType.SESSION, current.session_id),
        {"method": method, "reason": reason},
    )
    await db.commit()
    log.info("reauth_failed", user_id=current.user_id, method=method, reason=reason)


def _invalid(method: str) -> ProblemError:
    if method == PASSWORD:
        return ProblemError(
            400,
            detail="The password is not correct.",
            type="urn:ollamail:problem:reauth-invalid",
        )
    return mfa_service.invalid_code(status.HTTP_400_BAD_REQUEST)


# -- Endpoints ---------------------------------------------------------------------------

router = APIRouter(prefix="/auth/reauth", tags=["auth"])

_UNAUTHORIZED: dict[int | str, dict[str, Any]] = {401: {"description": "Not signed in"}}
_ATTEMPT: dict[int | str, dict[str, Any]] = {
    **_UNAUTHORIZED,
    400: {"description": "Wrong password, code or passkey"},
    409: {"description": "The method is not available for this account"},
    429: {"description": "Too many attempts"},
}


@router.get("", responses=_UNAUTHORIZED)
async def get_options(
    current: CurrentSessionDep, request: Request, db: DbDep, settings: SettingsDep
) -> ReauthOptions:
    """How the signed-in user can confirm who they are before a sensitive action."""
    methods: list[ReauthMethod] = []
    if await _local_password_hash(db, current.user_id):
        methods.append("password")
    found = await mfa_service.factors(db, current.user_id)
    if found.passkeys and relying_party(settings.auth) is not None:
        methods.append("webauthn")
    if found.totp:
        methods.append("totp")
    provider = await _redirect_provider(db, request, current)
    if provider is not None:
        methods.append("sso")
    methods.append("signin")
    return ReauthOptions(
        methods=methods,
        provider_display_name=provider.display_name if provider else None,
        login_path=provider.login_path if provider else None,
        reauth_minutes=settings.auth.reauth_minutes,
        valid_until=valid_until(settings, current.authenticated_at)
        if is_recent(settings, current)
        else None,
    )


@router.post("", responses=_ATTEMPT)
async def confirm(
    body: ReauthRequest, current: CurrentSessionDep, db: DbDep, settings: SettingsDep
) -> ReauthStatus:
    """Confirm with the password or an authenticator code."""
    await _throttle(db, settings, current.user_id)
    if body.method == PASSWORD:
        password_hash = await _local_password_hash(db, current.user_id)
        if password_hash is None:
            raise _unavailable()
        assert body.password is not None
        valid = await verify_password(password_hash, body.password)
    else:
        if not (await mfa_service.factors(db, current.user_id)).totp:
            raise _unavailable()
        assert body.code is not None
        valid = await mfa_service.check_totp(db, current.user_id, body.code.strip())
    if not valid:
        await _rejected(db, current, body.method, "invalid_credentials")
        raise _invalid(body.method)
    return await _confirmed(db, settings, current, body.method)


def _unavailable() -> ProblemError:
    return ProblemError(
        409,
        detail="This way of confirming is not available for the account.",
        type="urn:ollamail:problem:reauth-unavailable",
    )


@router.post("/passkey/options", responses=_ATTEMPT)
async def passkey_options(
    current: CurrentSessionDep,
    request: Request,
    response: Response,
    db: DbDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    """WebAuthn request options for confirming with a passkey."""
    rp = relying_party(settings.auth)
    passkeys = (await mfa_service.factors(db, current.user_id)).passkeys
    if rp is None or not passkeys:
        raise _unavailable()
    challenge = secrets.token_bytes(32)
    await pending_store.start(
        db,
        settings.auth,
        request,
        response,
        purpose=pending_store.REAUTH,
        user_id=current.user_id,
        session_id=current.session_id,
        webauthn_challenge=challenge,
    )
    await db.commit()
    allowed = [StoredCredential(p.credential_id, list(p.transports)) for p in passkeys]
    return authentication_options(rp, challenge=challenge, allowed=allowed, passwordless=False)


@router.post("/passkey", responses=_ATTEMPT)
async def confirm_passkey(
    body: PasskeyAssertion,
    current: CurrentSessionDep,
    request: Request,
    response: Response,
    db: DbDep,
    settings: SettingsDep,
) -> ReauthStatus:
    """Confirm with a passkey (after ``/auth/reauth/passkey/options``)."""
    rp = relying_party(settings.auth)
    if rp is None:
        raise _unavailable()
    await _throttle(db, settings, current.user_id)
    state = await pending_store.current(db, request, {pending_store.REAUTH})
    challenge = None
    if state is not None and state.session_id == current.session_id:
        challenge = state.webauthn_challenge
    if state is not None:
        # Single use, whatever the outcome.
        await db.delete(state)
    pending_store.clear_cookie(response, settings.auth)
    credential_id = credential_id_of(body.credential)
    passkey = (
        await db.scalar(
            select(Passkey).where(
                Passkey.user_id == current.user_id, Passkey.credential_id == credential_id
            )
        )
        if credential_id and challenge
        else None
    )
    sign_count = None
    if passkey is not None and challenge is not None:
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
            sign_count = None
    if passkey is None or sign_count is None:
        await _rejected(db, current, mfa_service.WEBAUTHN, "invalid_passkey")
        raise ProblemError(
            400,
            detail="The passkey could not be verified.",
            type="urn:ollamail:problem:passkey-invalid",
        )
    passkey.sign_count = sign_count
    passkey.last_used_at = datetime.now(UTC)
    return await _confirmed(db, settings, current, mfa_service.WEBAUTHN)
