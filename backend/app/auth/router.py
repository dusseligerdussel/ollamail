"""Endpoints for setup, login/logout, the own account and sessions."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import service
from app.auth import sessions as session_store
from app.auth.dependencies import CurrentSessionDep, CurrentUserDep, SettingsDep
from app.auth.models import LOCAL_PROVIDER
from app.auth.passwords import hash_password
from app.auth.providers import AuthProviderRegistry, LocalAuthProvider
from app.auth.schemas import (
    AuthProviderInfo,
    AuthProviders,
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
    },
)
async def create_admin(
    body: SetupRequest, request: Request, response: Response, db: DbDep, settings: SettingsDep
) -> UserRead:
    """Create the first admin and sign them in. Only possible while no user exists."""
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
    await service.start_session(db, settings, request, response, user, provider=LOCAL_PROVIDER)
    log.info("setup_completed", user_id=user.id)
    return UserRead.model_validate(user)


@router.get("/providers")
async def providers(request: Request, settings: SettingsDep) -> AuthProviders:
    """Sign-in options for the login page."""
    registry: AuthProviderRegistry = request.app.state.auth_providers
    return AuthProviders(
        local_login=True,
        local_registration=settings.auth.local_registration,
        providers=[
            AuthProviderInfo(name=p.name, display_name=p.display_name, kind=p.kind)
            for p in registry
        ],
    )


@router.post("/login", responses={401: {"description": "Wrong credentials"}, **_THROTTLED})
async def login(
    body: LoginRequest, request: Request, response: Response, db: DbDep, settings: SettingsDep
) -> UserRead:
    """Sign in with a local account."""
    await service.throttle_ip(db, settings, request)
    try:
        await service.throttle_account(db, settings, body.email)
    except ProblemError:
        log.warning("login_rejected", reason="locked")
        raise
    identity = await LocalAuthProvider(db).authenticate(body.email, body.password)
    user = await service.user_for_identity(db, identity) if identity else None
    if identity is None or user is None:
        log.info("login_failed", reason="invalid_credentials")
        raise ProblemError(401, detail="Invalid e-mail address or password.")
    await service.reset_account_throttle(db, settings, body.email)
    await service.start_session(db, settings, request, response, user, provider=identity.provider)
    log.info("login_succeeded", user_id=user.id, provider=identity.provider)
    return UserRead.model_validate(user)


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    responses={
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
) -> UserRead:
    """Create a local account (role ``user``) and sign in, if self-registration is on."""
    if not settings.auth.local_registration:
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


@router.get("/sessions", responses=_UNAUTHORIZED)
async def list_sessions(
    current: CurrentSessionDep, db: DbDep, settings: SettingsDep
) -> list[SessionRead]:
    """The own active sessions, most recently used first."""
    rows = await session_store.list_sessions(db, settings.auth, current.user_id)
    return [
        SessionRead(
            id=row.id,
            provider=row.provider,
            created_at=row.created_at,
            last_seen_at=row.last_seen_at,
            expires_at=row.expires_at,
            user_agent=row.user_agent,
            current=row.id == current.session_id,
        )
        for row in rows
    ]


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
    await db.commit()
    log.info("sessions_revoked", user_id=current.user_id, count=count)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    if include_current:
        service.clear_cookies(response, settings)
    return response
