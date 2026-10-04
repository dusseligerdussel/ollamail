"""Endpoints for setup, login/logout, the own account and sessions."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.auth import identities, link_notices, service
from app.auth import sessions as session_store
from app.auth.dependencies import CurrentSessionDep, CurrentUserDep, SettingsDep
from app.auth.mfa import service as mfa_service
from app.auth.mfa.passkeys import relying_party
from app.auth.mfa.router import second_step as mfa_second_step
from app.auth.mfa.schemas import MfaChallenge
from app.auth.models import LOCAL_PROVIDER
from app.auth.passwords import hash_password
from app.auth.policy import local_login_enabled
from app.auth.providers import (
    AuthProviderKind,
    AuthProviderRegistry,
    LocalAuthProvider,
    RedirectAuthProvider,
)
from app.auth.providers.ldap.service import list_directories
from app.auth.reauth import REAUTH_RESPONSES, RecentAuthDep
from app.auth.schemas import (
    AuthProviderInfo,
    AuthProviders,
    IdentityRead,
    LinkBlockRead,
    LinkNoticeRead,
    LoginRequest,
    RegisterRequest,
    SessionRead,
    SetupRequest,
    SetupStatus,
)
from app.auth.setup import already_initialized, create_first_admin, setup_token_valid
from app.core.db import get_db
from app.core.errors import ProblemError
from app.core.logging import get_logger
from app.users.models import UserRole
from app.users.schemas import ProfileUpdate, UserRead
from app.users.service import add_local_user, any_user_exists, check_password_policy

log = get_logger(__name__)

DbDep = Annotated[AsyncSession, Depends(get_db)]

setup_router = APIRouter(prefix="/setup", tags=["setup"])
router = APIRouter(prefix="/auth", tags=["auth"])

_UNAUTHORIZED: dict[int | str, dict[str, Any]] = {401: {"description": "Not signed in"}}
_THROTTLED: dict[int | str, dict[str, Any]] = {
    429: {"description": "Too many attempts (rate limit or account lockout)"}
}
# Audit details of failed local logins (no e-mail address: docs/PRIVACY.md).
LOCKED = {"provider": LOCAL_PROVIDER, "reason": "locked"}
INVALID = {"provider": LOCAL_PROVIDER, "reason": "invalid_credentials"}


@setup_router.get("/status")
async def get_status(db: DbDep) -> SetupStatus:
    """Whether the instance has been set up (an admin exists)."""
    return SetupStatus(initialized=await any_user_exists(db))


@setup_router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    responses={
        403: {"description": "Wrong setup token"},
        409: {"description": "The instance is already set up"},
        **_THROTTLED,
    },
)
async def create_admin(
    body: SetupRequest, request: Request, response: Response, db: DbDep, settings: SettingsDep
) -> UserRead:
    """Create the first admin and sign them in. Only possible while no user exists.
    Attempts count towards the per-IP limit of the login (``OLLAMAIL_AUTH_IP_MAX_ATTEMPTS``)."""
    await service.throttle_ip(db, settings, request)
    if await any_user_exists(db):
        raise already_initialized()
    if not setup_token_valid(settings, body.setup_token):
        log.warning("setup_rejected", reason="invalid_setup_token")
        raise ProblemError(403, detail="The setup token is invalid.")
    check_password_policy(settings.auth, body.password)
    # Hash before taking the lock: the lock is only held for the check and the insert.
    password_hash = await hash_password(body.password)
    user = await create_first_admin(
        db,
        email=body.email,
        display_name=body.display_name,
        password_hash=password_hash,
        language=body.language,
        timezone=body.timezone,
    )
    await audit.record(db, audit.Actor.user(user.id), audit.AuditAction.SETUP_COMPLETED)
    await service.start_session(db, settings, request, response, user, provider=LOCAL_PROVIDER)
    log.info("setup_completed", user_id=user.id)
    return UserRead.model_validate(user)


@router.get("/providers")
async def providers(request: Request, db: DbDep, settings: SettingsDep) -> AuthProviders:
    """Sign-in options for the login page."""
    registry: AuthProviderRegistry = request.app.state.auth_providers
    infos = [
        AuthProviderInfo(
            name=p.name,
            display_name=p.display_name,
            kind=p.kind,
            login_path=p.login_path if isinstance(p, RedirectAuthProvider) else None,
        )
        for p in await registry.available(db)
    ]
    # LDAP directories are configured in the database (sign-in: POST /auth/login/ldap/{name}).
    infos += [
        AuthProviderInfo(
            name=d.provider, display_name=d.display_name, kind=AuthProviderKind.PASSWORD
        )
        for d in await list_directories(db, enabled_only=True)
    ]
    local = await local_login_enabled(db)
    return AuthProviders(
        local_login=local,
        local_registration=local and settings.auth.local_registration,
        passkey_login=local and relying_party(settings.auth) is not None,
        providers=infos,
    )


def _local_login_disabled() -> ProblemError:
    return ProblemError(
        403,
        detail="Sign-in with local accounts is disabled.",
        type="urn:ollamail:problem:local-login-disabled",
    )


@router.post(
    "/login",
    response_model=UserRead,
    responses={
        202: {"model": MfaChallenge, "description": "Password correct, second step needed"},
        401: {"description": "Wrong credentials"},
        403: {"description": "Local login is disabled"},
        **_THROTTLED,
    },
)
async def login(
    body: LoginRequest, request: Request, response: Response, db: DbDep, settings: SettingsDep
) -> UserRead | JSONResponse:
    """Sign in with a local account (unless an admin switched local login off).

    Accounts with a second factor (or that must set one up) get 202 and no session yet;
    the login continues under ``/auth/mfa`` (app/auth/mfa/router.py)."""
    if not await local_login_enabled(db):
        raise _local_login_disabled()
    await service.throttle_ip(db, settings, request)
    try:
        await service.throttle_account(db, settings, body.email)
    except ProblemError:
        log.warning("login_rejected", reason="locked")
        await audit.record(db, audit.ANONYMOUS, audit.AuditAction.LOGIN_FAILED, None, LOCKED)
        await db.commit()
        raise
    identity = await LocalAuthProvider(db).authenticate(body.email, body.password)
    user = await service.user_for_identity(db, identity) if identity else None
    if identity is None or user is None:
        log.info("login_failed", reason="invalid_credentials")
        await audit.record(db, audit.ANONYMOUS, audit.AuditAction.LOGIN_FAILED, None, INVALID)
        await db.commit()
        raise ProblemError(401, detail="Invalid e-mail address or password.")
    await service.reset_account_throttle(db, settings, body.email)
    step = await mfa_service.login_step(db, user)
    if step is not None:
        return await mfa_second_step(db, settings, request, user, step)
    await audit.record(
        db,
        audit.Actor.user(user.id),
        audit.AuditAction.LOGIN_SUCCEEDED,
        details={"provider": identity.provider},
    )
    await service.start_session(db, settings, request, response, user, provider=identity.provider)
    log.info("login_succeeded", user_id=user.id, provider=identity.provider)
    return UserRead.model_validate(user)


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    response_model=UserRead,
    responses={
        202: {"model": MfaChallenge, "description": "Account created, 2FA must be set up"},
        403: {"description": "Registration is disabled"},
        409: {"description": "E-mail address taken or instance not set up"},
        **_THROTTLED,
    },
)
async def register(
    body: RegisterRequest,
    request: Request,
    response: Response,
    db: DbDep,
    settings: SettingsDep,
) -> UserRead | JSONResponse:
    """Create a local account (role ``user``) and sign in, if self-registration is on.
    While 2FA is enforced for all accounts, the answer is 202 and the account sets up a
    factor first (as at the login)."""
    if not settings.auth.local_registration or not await local_login_enabled(db):
        raise ProblemError(403, detail="Registration is disabled.")
    if not await any_user_exists(db):
        raise ProblemError(
            409,
            detail="The instance is not set up yet.",
            type="urn:ollamail:problem:not-initialized",
        )
    await service.throttle_ip(db, settings, request)
    check_password_policy(settings.auth, body.password)
    user = await add_local_user(
        db,
        email=body.email,
        display_name=body.display_name,
        password_hash=await hash_password(body.password),
        role=UserRole.USER,
        language=body.language,
        timezone=body.timezone,
    )
    await audit.record(
        db,
        audit.Actor.user(user.id),
        audit.AuditAction.USER_CREATED,
        audit.Target.of(audit.TargetType.USER, user.id),
        {"role": user.role, "via": "registration"},
    )
    step = await mfa_service.login_step(db, user)
    if step is not None:
        # Commits the new account together with the pending enrolment.
        log.info("user_registered", user_id=user.id, second_step=step)
        return await mfa_second_step(db, settings, request, user, step)
    await service.start_session(db, settings, request, response, user, provider=LOCAL_PROVIDER)
    log.info("user_registered", user_id=user.id)
    return UserRead.model_validate(user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, db: DbDep, settings: SettingsDep) -> Response:
    """End the current session (also without a valid session)."""
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    await service.end_session(db, settings, request, response)
    return response


@router.get("/me", responses=_UNAUTHORIZED)
async def me(user: CurrentUserDep) -> UserRead:
    """The signed-in user."""
    return UserRead.model_validate(user)


@router.patch("/me", responses=_UNAUTHORIZED)
async def update_me(body: ProfileUpdate, user: CurrentUserDep, db: DbDep) -> UserRead:
    """Change display name, language or time zone of the own account."""
    for field, value in body.model_dump(exclude_unset=True, exclude_none=True).items():
        setattr(user, field, value)
    await db.commit()
    await db.refresh(user)
    return UserRead.model_validate(user)


async def _provider_names(request: Request, db: AsyncSession) -> dict[str, str]:
    """Display names of the configured external providers by key (incl. disabled LDAP
    directories, so older sessions keep their name)."""
    registry: AuthProviderRegistry = request.app.state.auth_providers
    names = {d.provider: d.display_name for d in await list_directories(db)}
    names.update({p.name: p.display_name for p in await registry.available(db)})
    return names


@router.get("/sessions", responses=_UNAUTHORIZED)
async def list_sessions(
    request: Request, current: CurrentSessionDep, db: DbDep, settings: SettingsDep
) -> list[SessionRead]:
    """The own active sessions, most recently used first, with the sign-in method of each."""
    rows = await session_store.list_sessions(db, settings.auth, current.user_id)
    names = await _provider_names(request, db) if rows else {}
    return [
        SessionRead(
            id=row.id,
            provider=row.provider,
            provider_name=names.get(row.provider),
            created_at=row.created_at,
            last_seen_at=row.last_seen_at,
            expires_at=row.expires_at,
            user_agent=row.user_agent,
            current=row.id == current.session_id,
        )
        for row in rows
    ]


@router.get("/link-notices", responses=_UNAUTHORIZED)
async def list_link_notices(
    request: Request, current: CurrentSessionDep, db: DbDep
) -> list[LinkNoticeRead]:
    """Sign-ins linked to the own account by e-mail address that are not acknowledged yet
    (#208), newest first. Sessions of the linked provider itself do not see them."""
    rows = await link_notices.list_link_notices(db, current.user_id, current.session_id)
    names = await _provider_names(request, db) if rows else {}
    return [
        LinkNoticeRead(
            id=row.id,
            provider=row.provider,
            provider_name=names.get(row.provider),
            created_at=row.created_at,
        )
        for row in rows
    ]


@router.delete(
    "/link-notices/{notice_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={**_UNAUTHORIZED, 404: {"description": "No such notice"}},
)
async def dismiss_link_notice(
    notice_id: uuid.UUID, current: CurrentSessionDep, db: DbDep
) -> Response:
    """Acknowledge a link notice. Not possible from a session of the linked provider."""
    provider = await link_notices.dismiss_link_notice(
        db, current.user_id, current.session_id, notice_id
    )
    if provider is None:
        raise ProblemError(404, detail="Notice not found.")
    await db.commit()
    log.info("identity_link_notice_dismissed", user_id=current.user_id, provider=provider)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


_UNLINK_REFUSALS = {
    identities.UnlinkRefusal.LOCAL: "Local sign-in cannot be unlinked.",
    identities.UnlinkRefusal.CURRENT_SESSION: (
        "This session signed in with this method. Sign in another way to unlink it."
    ),
    identities.UnlinkRefusal.LAST_SIGN_IN: "This is the last way to sign in to the account.",
}


@router.get("/identities", responses=_UNAUTHORIZED)
async def list_identities(
    request: Request, current: CurrentSessionDep, db: DbDep
) -> list[IdentityRead]:
    """The own ways to sign in (without SCIM), oldest first, with whether each can be
    unlinked (#216)."""
    methods = await identities.list_sign_in_methods(db, current.user_id, current.session_id)
    names = await _provider_names(request, db) if methods else {}
    return [
        IdentityRead(
            id=method.identity.id,
            provider=method.identity.provider,
            provider_name=names.get(method.identity.provider),
            created_at=method.identity.created_at,
            last_used_at=method.identity.last_used_at,
            current=method.current,
            unlink_refusal=method.refusal,
        )
        for method in methods
    ]


@router.delete(
    "/identities/{identity_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        **_UNAUTHORIZED,
        **REAUTH_RESPONSES,
        404: {"description": "No such sign-in method"},
        409: {"description": "Local sign-in, the current session's method or the last one"},
    },
)
async def unlink_identity(identity_id: uuid.UUID, current: RecentAuthDep, db: DbDep) -> Response:
    """Unlink an external sign-in (#216): ends its sessions, drops the notices about it and
    blocks the provider from linking to the account by e-mail address again."""
    method = await identities.find_sign_in_method(
        db, current.user_id, current.session_id, identity_id
    )
    if method is None:
        raise ProblemError(404, detail="Sign-in method not found.")
    if method.refusal is not None:
        raise ProblemError(
            409,
            detail=_UNLINK_REFUSALS[method.refusal],
            type=f"urn:ollamail:problem:unlink-{method.refusal.value.replace('_', '-')}",
            reason=method.refusal.value,
        )
    unlinked = await identities.unlink(db, current.user_id, method.identity)
    await audit.record(
        db,
        audit.Actor.user(current.user_id),
        audit.AuditAction.USER_IDENTITY_UNLINKED,
        audit.Target.of(audit.TargetType.USER, current.user_id),
        {"provider": unlinked.provider, "sessions": unlinked.sessions},
    )
    await db.commit()
    log.info(
        "identity_unlinked",
        user_id=current.user_id,
        provider=unlinked.provider,
        sessions=unlinked.sessions,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/link-blocks", responses=_UNAUTHORIZED)
async def list_link_blocks(
    request: Request, current: CurrentSessionDep, db: DbDep
) -> list[LinkBlockRead]:
    """Providers blocked from linking to the own account by e-mail address because the user
    unlinked them (#216), newest first."""
    rows = await identities.list_link_blocks(db, current.user_id, current.session_id)
    names = await _provider_names(request, db) if rows else {}
    return [
        LinkBlockRead(
            id=row.id,
            provider=row.provider,
            provider_name=names.get(row.provider),
            created_at=row.created_at,
        )
        for row in rows
    ]


@router.delete(
    "/link-blocks/{block_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={**_UNAUTHORIZED, **REAUTH_RESPONSES, 404: {"description": "No such block"}},
)
async def lift_link_block(block_id: uuid.UUID, current: RecentAuthDep, db: DbDep) -> Response:
    """Lift a block: the provider links to the account by e-mail address again at its next
    sign-in. Only the user can do this, not from a session of the blocked provider."""
    provider = await identities.lift_link_block(db, current.user_id, current.session_id, block_id)
    if provider is None:
        raise ProblemError(404, detail="Block not found.")
    await audit.record(
        db,
        audit.Actor.user(current.user_id),
        audit.AuditAction.USER_IDENTITY_LINK_UNBLOCKED,
        audit.Target.of(audit.TargetType.USER, current.user_id),
        {"provider": provider},
    )
    await db.commit()
    log.info("identity_link_unblocked", user_id=current.user_id, provider=provider)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={**_UNAUTHORIZED, 404: {"description": "No such session"}},
)
async def revoke_session(
    session_id: uuid.UUID,
    request: Request,
    current: CurrentSessionDep,
    db: DbDep,
    settings: SettingsDep,
) -> Response:
    """Sign out one of the own sessions."""
    if not await session_store.revoke_session(db, current.user_id, session_id):
        raise ProblemError(404, detail="Session not found.")
    await audit.record(
        db,
        audit.Actor.user(current.user_id),
        audit.AuditAction.SESSION_REVOKED,
        audit.Target.of(audit.TargetType.SESSION, session_id),
        {"count": 1, "current": session_id == current.session_id},
    )
    await db.commit()
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    if session_id == current.session_id:
        service.clear_cookies(response, settings)
    return response


@router.delete("/sessions", status_code=status.HTTP_204_NO_CONTENT, responses=_UNAUTHORIZED)
async def revoke_sessions(
    current: CurrentSessionDep,
    db: DbDep,
    settings: SettingsDep,
    include_current: Annotated[bool, Query()] = False,
) -> Response:
    """Sign out all other sessions, or all sessions with ``include_current=true``."""
    count = await session_store.revoke_user_sessions(
        db, current.user_id, keep=None if include_current else current.session_id
    )
    await audit.record(
        db,
        audit.Actor.user(current.user_id),
        audit.AuditAction.SESSION_REVOKED,
        audit.Target.of(audit.TargetType.USER, current.user_id),
        {"count": count, "current": include_current},
    )
    await db.commit()
    log.info("sessions_revoked", user_id=current.user_id, count=count)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    if include_current:
        service.clear_cookies(response, settings)
    return response
