"""LDAP login and directory administration (admin only; admin UI: Admin → Sign-in)."""

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.auth import service
from app.auth.admin_access import AdminAccessGuard
from app.auth.dependencies import AdminSessionDep, SettingsDep
from app.auth.providers.ldap import service as ldap_service
from app.auth.providers.ldap.client import LdapError
from app.auth.providers.ldap.models import LdapDirectory
from app.auth.providers.ldap.provider import is_allowed
from app.auth.providers.ldap.schemas import (
    LdapConnectionTest,
    LdapDirectoryCreate,
    LdapDirectoryRead,
    LdapDirectoryUpdate,
    LdapLoginRequest,
    LdapServerCheck,
    LdapUserLookup,
    LdapUserLookupRequest,
)
from app.auth.provisioning import (
    ProvisioningError,
    ProvisioningErrorCode,
    ProvisioningPolicy,
    provision_user,
)
from app.core.db import get_db
from app.core.errors import ProblemError
from app.core.logging import get_logger
from app.users.schemas import UserRead

log = get_logger(__name__)

DbDep = Annotated[AsyncSession, Depends(get_db)]

login_router = APIRouter(prefix="/auth/login/ldap", tags=["auth"])
router = APIRouter(
    prefix="/auth/ldap/directories",
    tags=["auth"],
    responses={401: {"description": "Not signed in"}, 403: {"description": "Not an admin"}},
)

_NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"description": "No such directory"}}


def _read(directory: LdapDirectory) -> LdapDirectoryRead:
    return LdapDirectoryRead(
        id=directory.id,
        name=directory.name,
        provider=directory.provider,
        display_name=directory.display_name,
        enabled=directory.enabled,
        settings=ldap_service.directory_settings(directory),
        bind_password_set=bool(directory.bind_password),
        created_at=directory.created_at,
        updated_at=directory.updated_at,
    )


async def _directory(db: AsyncSession, name: str) -> LdapDirectory:
    directory = await ldap_service.get_directory(db, name)
    if directory is None:
        raise ProblemError(404, detail="Directory not found.")
    return directory


async def _login_failed(db: AsyncSession, provider: str, reason: str) -> None:
    await audit.record(
        db,
        audit.ANONYMOUS,
        audit.AuditAction.LOGIN_FAILED,
        details={"provider": provider, "reason": reason},
    )
    await db.commit()


async def _config_changed(db: AsyncSession, admin_id: Any, directory_id: Any, change: str) -> None:
    await audit.record(
        db,
        audit.Actor.user(admin_id),
        audit.AuditAction.IDP_CONFIG_CHANGED,
        audit.Target.of(audit.TargetType.IDP, directory_id),
        details={"kind": "ldap", "change": change},
    )


def _unavailable() -> ProblemError:
    return ProblemError(
        503,
        detail="The directory service is not available. Try again later.",
        type="urn:ollamail:problem:directory-unavailable",
    )


@login_router.post(
    "/{name}",
    responses={
        401: {"description": "Wrong credentials, account disabled or not allowed"},
        403: {"description": "The directory supplies no e-mail address for the user"},
        404: {"description": "No such (enabled) directory"},
        409: {"description": "E-mail address belongs to an account not linked to the directory"},
        429: {"description": "Too many attempts (rate limit or account lockout)"},
        503: {"description": "Directory not reachable"},
    },
)
async def ldap_login(
    name: str,
    body: LdapLoginRequest,
    request: Request,
    response: Response,
    db: DbDep,
    settings: SettingsDep,
) -> UserRead:
    """Sign in with a directory account; the user is created on the first login."""
    await service.throttle_ip(db, settings, request)
    directory = await ldap_service.get_directory(db, name)
    if directory is None or not directory.enabled:
        raise ProblemError(404, detail="Directory not found.")
    # Same lockout as local accounts, counted per directory and user name.
    account = f"{directory.provider}:{body.username}"
    try:
        await service.throttle_account(db, settings, account)
    except ProblemError:
        log.warning("login_rejected", reason="locked", provider=directory.provider)
        await _login_failed(db, directory.provider, "locked")
        raise
    try:
        provider = ldap_service.provider_for(directory, settings.auth)
    except ProblemError:
        log.error("ldap_login_unavailable", provider=directory.provider, error="plaintext")
        await _login_failed(db, directory.provider, "directory_unavailable")
        raise _unavailable() from None
    try:
        identity = await provider.authenticate(body.username, body.password)
    except LdapError as exc:
        log.error("ldap_login_unavailable", provider=directory.provider, error=exc.code)
        await _login_failed(db, directory.provider, "directory_unavailable")
        raise _unavailable() from None
    if identity is None:
        log.info("login_failed", reason="invalid_credentials", provider=directory.provider)
        await _login_failed(db, directory.provider, "invalid_credentials")
        raise ProblemError(401, detail="Invalid user name or password.")
    try:
        # Groups are mapped to the role at every login and stored for group assignments
        # of shared mailboxes (#34, docs/auth/ldap.md).
        result = await provision_user(
            db, identity, ProvisioningPolicy(), role=provider.role(identity)
        )
    except ProvisioningError as exc:
        if exc.code is not ProvisioningErrorCode.INACTIVE:
            raise
        log.info("login_failed", reason="user_inactive", provider=directory.provider)
        await _login_failed(db, directory.provider, "user_inactive")
        raise ProblemError(401, detail="Invalid user name or password.") from None
    user = result.user
    await service.reset_account_throttle(db, settings, account)
    await audit.record(
        db,
        audit.Actor.user(user.id),
        audit.AuditAction.LOGIN_SUCCEEDED,
        details={"provider": identity.provider},
    )
    await service.start_session(db, settings, request, response, user, provider=identity.provider)
    log.info("login_succeeded", user_id=user.id, provider=identity.provider)
    return UserRead.model_validate(user)


@router.get("")
async def list_directories(_: AdminSessionDep, db: DbDep) -> list[LdapDirectoryRead]:
    """All configured directories (without bind passwords)."""
    return [_read(directory) for directory in await ldap_service.list_directories(db)]


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    responses={409: {"description": "Name taken"}, 422: {"description": "Invalid settings"}},
)
async def create_directory(
    body: LdapDirectoryCreate, admin: AdminSessionDep, db: DbDep, settings: SettingsDep
) -> LdapDirectoryRead:
    """Add a directory. Test it with ``…/test`` before enabling it for users."""
    ldap_service.check_transport_security(settings.auth, body.settings)
    if await ldap_service.get_directory(db, body.name) is not None:
        raise _name_taken()
    directory = LdapDirectory(
        name=body.name,
        display_name=body.display_name,
        enabled=body.enabled,
        settings=body.settings.model_dump(mode="json"),
        bind_password=body.bind_password,
    )
    db.add(directory)
    try:
        await db.flush()
        await _config_changed(db, admin.user_id, directory.id, "created")
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise _name_taken() from None
    await db.refresh(directory)
    log.info("ldap_directory_created", directory_id=directory.id, by_user_id=admin.user_id)
    return _read(directory)


def _name_taken() -> ProblemError:
    return ProblemError(
        409, detail="A directory with this name exists.", type="urn:ollamail:problem:name-taken"
    )


@router.get("/{name}", responses=_NOT_FOUND)
async def get_directory(name: str, _: AdminSessionDep, db: DbDep) -> LdapDirectoryRead:
    return _read(await _directory(db, name))


_LOCKOUT: dict[int | str, dict[str, Any]] = {
    409: {"description": "No administrator could sign in afterwards (admin-lockout)"}
}


@router.put(
    "/{name}",
    responses={**_NOT_FOUND, **_LOCKOUT, 422: {"description": "Invalid settings"}},
)
async def update_directory(
    name: str,
    body: LdapDirectoryUpdate,
    admin: AdminSessionDep,
    request: Request,
    db: DbDep,
    settings: SettingsDep,
) -> LdapDirectoryRead:
    """Replace the configuration. Without ``bind_password`` the stored one is kept."""
    ldap_service.check_transport_security(settings.auth, body.settings)
    guard = await AdminAccessGuard.start(db, request.app.state.auth_providers)
    directory = await _directory(db, name)
    directory.display_name = body.display_name
    directory.enabled = body.enabled
    directory.settings = body.settings.model_dump(mode="json")
    if body.bind_password is not None:
        directory.bind_password = body.bind_password
    await guard.check()
    await _config_changed(db, admin.user_id, directory.id, "updated")
    await db.commit()
    await db.refresh(directory)
    log.info("ldap_directory_updated", directory_id=directory.id, by_user_id=admin.user_id)
    return _read(directory)


@router.delete(
    "/{name}", status_code=status.HTTP_204_NO_CONTENT, responses={**_NOT_FOUND, **_LOCKOUT}
)
async def delete_directory(
    name: str, admin: AdminSessionDep, request: Request, db: DbDep
) -> Response:
    """Remove the directory and all sign-in links through it (the users stay)."""
    guard = await AdminAccessGuard.start(db, request.app.state.auth_providers)
    directory = await _directory(db, name)
    directory_id = directory.id
    await ldap_service.delete_directory(db, directory)
    await guard.check()
    await _config_changed(db, admin.user_id, directory_id, "deleted")
    await db.commit()
    log.info("ldap_directory_deleted", directory_id=directory_id, by_user_id=admin.user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{name}/test", responses=_NOT_FOUND)
async def test_connection(
    name: str, _: AdminSessionDep, db: DbDep, settings: SettingsDep
) -> LdapConnectionTest:
    """Connect to every server, negotiate TLS and bind as the service account."""
    provider = ldap_service.provider_for(await _directory(db, name), settings.auth)
    checks = await asyncio.to_thread(provider.client.check_servers)
    return LdapConnectionTest(
        ok=any(check.ok for check in checks),
        servers=[
            LdapServerCheck(
                url=check.url, ok=check.ok, error=check.error, latency_ms=check.latency_ms
            )
            for check in checks
        ],
    )


@router.post("/{name}/test-user", responses=_NOT_FOUND)
async def test_user(
    name: str,
    body: LdapUserLookupRequest,
    _: AdminSessionDep,
    db: DbDep,
    settings: SettingsDep,
) -> LdapUserLookup:
    """Look up a user as a login would (search, attributes, groups, role), without
    checking a password."""
    provider = ldap_service.provider_for(await _directory(db, name), settings.auth)
    try:
        user = await asyncio.to_thread(provider.client.find_user, body.login)
    except LdapError as exc:
        return LdapUserLookup(found=False, error=exc.code)
    if user is None:
        return LdapUserLookup(found=False)
    identity = provider.identity(user)
    return LdapUserLookup(
        found=True,
        dn=user.dn,
        subject=user.subject,
        email=user.email,
        display_name=user.display_name,
        groups=sorted(user.groups),
        disabled=user.disabled,
        allowed=is_allowed(provider.settings, user.groups),
        role=provider.role(identity),
    )
