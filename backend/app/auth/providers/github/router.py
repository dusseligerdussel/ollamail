"""GitHub endpoints: browser login/callback and provider administration (admin only)."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.auth import redirect_flow
from app.auth.admin_access import AdminAccessGuard
from app.auth.dependencies import AdminSessionDep, SettingsDep
from app.auth.providers.github import store
from app.auth.providers.github.config import from_record, normalize_base_url
from app.auth.providers.github.errors import GitHubError
from app.auth.providers.github.models import GitHubProviderRecord
from app.auth.providers.github.schemas import (
    GitHubProviderCreate,
    GitHubProviderRead,
    GitHubProviderUpdate,
)
from app.auth.reauth import ADMIN_REAUTH_RESPONSES, RecentAdminDep
from app.core.config import Settings
from app.core.db import get_db
from app.core.errors import ProblemError
from app.core.logging import get_logger

log = get_logger(__name__)

DbDep = Annotated[AsyncSession, Depends(get_db)]

router = APIRouter(prefix="/auth/github", tags=["auth"])
admin_router = APIRouter(
    prefix="/admin/auth/github",
    tags=["admin"],
    responses={401: {"description": "Not signed in"}, 403: {"description": "Not an admin"}},
)

_REDIRECT: dict[int | str, dict[str, Any]] = {
    303: {"description": "Redirect to GitHub, or to /login?error=<code>"}
}
_CALLBACK: dict[int | str, dict[str, Any]] = {
    303: {"description": "Redirect to return_to (signed in), or to /login?error=<code>"}
}


# -- Browser flow -----------------------------------------------------------------------


@router.get(
    "/{name}/login",
    status_code=status.HTTP_303_SEE_OTHER,
    response_class=RedirectResponse,
    responses=_REDIRECT,
)
async def github_login(
    name: str,
    request: Request,
    db: DbDep,
    settings: SettingsDep,
    return_to: Annotated[str | None, Query(max_length=2048)] = None,
) -> RedirectResponse:
    """Start the login with a GitHub provider (browser navigation, not fetch)."""
    provider = await store.enabled_provider(db, name)
    if provider is None:
        return redirect_flow.error_redirect(redirect_flow.FlowErrorCode.PROVIDER_UNKNOWN)
    return await redirect_flow.start_login(
        request,
        db,
        settings,
        provider,
        callback_path=provider.callback_path,
        return_to=return_to,
        error_type=GitHubError,
    )


@router.get(
    "/{name}/callback",
    status_code=status.HTTP_303_SEE_OTHER,
    response_class=RedirectResponse,
    responses=_CALLBACK,
)
async def github_callback(
    name: str, request: Request, db: DbDep, settings: SettingsDep
) -> RedirectResponse:
    """Redirect target of GitHub: checks the response and membership, signs the user in."""
    provider = await store.enabled_provider(db, name)
    if provider is None:
        return redirect_flow.error_redirect(redirect_flow.FlowErrorCode.PROVIDER_UNKNOWN)
    return await redirect_flow.finish_login(
        request, db, settings, provider, provider.provisioning, error_type=GitHubError
    )


# -- Administration ---------------------------------------------------------------------


def _read(record: GitHubProviderRecord, request: Request, settings: Settings) -> GitHubProviderRead:
    config = from_record(record)
    return GitHubProviderRead(
        name=config.name,
        provider=config.provider_name,
        display_name=config.display_name,
        base_url=config.base_url,
        client_id=config.client_id,
        has_client_secret=bool(config.client_secret),
        enabled=config.enabled,
        auto_provision=config.auto_provision,
        link_by_email=config.link_by_email,
        allowed_domains=sorted(config.allowed_domains),
        allowed_organizations=sorted(config.allowed_organizations),
        allowed_teams=sorted(config.allowed_teams),
        redirect_uri=redirect_flow.api_url(
            settings, request, f"/auth/github/{config.name}/callback"
        ),
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


async def _config_changed(db: AsyncSession, admin_id: Any, record_id: Any, change: str) -> None:
    await audit.record(
        db,
        audit.Actor.user(admin_id),
        audit.AuditAction.IDP_CONFIG_CHANGED,
        audit.Target.of(audit.TargetType.IDP, record_id),
        details={"kind": "github", "change": change},
    )


def _not_found() -> ProblemError:
    return ProblemError(404, detail="GitHub provider not found.")


def _normalize(record: GitHubProviderRecord) -> None:
    """Canonical base URL and sorted, distinct lists; 422 for an unusable base URL."""
    try:
        record.base_url = normalize_base_url(record.base_url)
    except ValueError as exc:
        # Static reason from the validator; it contains no secrets.
        raise ProblemError(
            422, detail=str(exc), type="urn:ollamail:problem:github-provider-invalid"
        ) from None
    record.allowed_domains = sorted(set(record.allowed_domains))
    record.allowed_organizations = sorted(set(record.allowed_organizations))
    record.allowed_teams = sorted(set(record.allowed_teams))


async def _record(db: AsyncSession, name: str) -> GitHubProviderRecord:
    record = await store.get_record(db, name)
    if record is None:
        raise _not_found()
    return record


@admin_router.get("/providers")
async def list_github_providers(
    _: AdminSessionDep, request: Request, db: DbDep, settings: SettingsDep
) -> list[GitHubProviderRead]:
    """All GitHub providers (secrets are never returned)."""
    return [_read(r, request, settings) for r in await store.records(db)]


@admin_router.get("/providers/{name}", responses={404: {"description": "Unknown provider"}})
async def get_github_provider(
    name: str, _: AdminSessionDep, request: Request, db: DbDep, settings: SettingsDep
) -> GitHubProviderRead:
    return _read(await _record(db, name), request, settings)


@admin_router.post(
    "/providers",
    status_code=status.HTTP_201_CREATED,
    responses={
        **ADMIN_REAUTH_RESPONSES,
        409: {"description": "Name taken"},
        422: {"description": "Invalid settings"},
    },
)
async def create_github_provider(
    body: GitHubProviderCreate,
    admin: RecentAdminDep,
    request: Request,
    db: DbDep,
    settings: SettingsDep,
) -> GitHubProviderRead:
    """Add a GitHub provider. The client secret is stored encrypted."""
    taken = ProblemError(
        409, detail="The name is already taken.", type="urn:ollamail:problem:name-taken"
    )
    if await store.get_record(db, body.name) is not None:
        raise taken
    record = GitHubProviderRecord(**body.model_dump())
    _normalize(record)
    db.add(record)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise taken from None
    await _config_changed(db, admin.user_id, record.id, "created")
    await db.commit()
    await db.refresh(record)
    log.info("github_provider_created", provider_name=record.name, by_user_id=admin.user_id)
    return _read(record, request, settings)


# Nullable settings where an explicit null clears the value (base_url: back to github.com).
_CLEARABLE = frozenset({"base_url"})


@admin_router.patch(
    "/providers/{name}",
    responses={
        **ADMIN_REAUTH_RESPONSES,
        404: {"description": "Unknown provider"},
        409: {"description": "No administrator could sign in afterwards (admin-lockout)"},
        422: {"description": "Invalid settings"},
    },
)
async def update_github_provider(
    name: str,
    body: GitHubProviderUpdate,
    admin: RecentAdminDep,
    request: Request,
    db: DbDep,
    settings: SettingsDep,
) -> GitHubProviderRead:
    """Change a GitHub provider. Omitted fields stay as they are."""
    guard = await AdminAccessGuard.start(db, request.app.state.auth_providers)
    record = await _record(db, name)
    for field in body.model_fields_set:
        value = getattr(body, field)
        if value is None and field not in _CLEARABLE:
            continue
        setattr(record, field, value)
    try:
        _normalize(record)
    except ProblemError:
        await db.rollback()
        raise
    await guard.check()
    await _config_changed(db, admin.user_id, record.id, "updated")
    await db.commit()
    await db.refresh(record)
    log.info("github_provider_updated", provider_name=name, by_user_id=admin.user_id)
    return _read(record, request, settings)


@admin_router.delete(
    "/providers/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        **ADMIN_REAUTH_RESPONSES,
        404: {"description": "Unknown provider"},
        409: {"description": "No administrator could sign in afterwards (admin-lockout)"},
    },
)
async def delete_github_provider(
    name: str, admin: RecentAdminDep, request: Request, db: DbDep
) -> None:
    """Remove a GitHub provider. Users and their linked identities are kept; sessions
    started with the provider stay valid until they expire or are revoked. Needs a recent
    confirmation (#218): removing a provider can lock its users out."""
    guard = await AdminAccessGuard.start(db, request.app.state.auth_providers)
    record = await _record(db, name)
    record_id = record.id
    await db.delete(record)
    await guard.check()
    await _config_changed(db, admin.user_id, record_id, "deleted")
    await db.commit()
    log.info("github_provider_deleted", provider_name=name, by_user_id=admin.user_id)
