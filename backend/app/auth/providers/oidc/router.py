"""OIDC endpoints: browser login/callback, logout and provider administration (admin only)."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.auth import redirect_flow, service
from app.auth.admin_access import AdminAccessGuard
from app.auth.dependencies import AdminSessionDep, SettingsDep
from app.auth.models import AuthSession
from app.auth.providers.oidc.config import OIDCConfig, validate_config
from app.auth.providers.oidc.errors import OIDCError
from app.auth.providers.oidc.metadata import MetadataCache
from app.auth.providers.oidc.models import OIDCProviderRecord
from app.auth.providers.oidc.presets import PRESETS
from app.auth.providers.oidc.provider import OIDCProvider
from app.auth.providers.oidc.schemas import (
    LogoutResult,
    OIDCConnectionTest,
    OIDCPresetRead,
    OIDCProviderCreate,
    OIDCProviderRead,
    OIDCProviderUpdate,
)
from app.auth.providers.oidc.store import OIDCProviderStore
from app.auth.reauth import ADMIN_REAUTH_RESPONSES, RecentAdminDep
from app.auth.sessions import SESSION_COOKIE, hash_token
from app.core.db import get_db
from app.core.errors import ProblemError
from app.core.logging import get_logger

log = get_logger(__name__)

DbDep = Annotated[AsyncSession, Depends(get_db)]

router = APIRouter(prefix="/auth/oidc", tags=["auth"])
admin_router = APIRouter(
    prefix="/admin/auth/oidc",
    tags=["admin"],
    responses={401: {"description": "Not signed in"}, 403: {"description": "Not an admin"}},
)

_REDIRECT: dict[int | str, dict[str, Any]] = {
    303: {"description": "Redirect to the IdP, or to /login?error=<code>"}
}
_CALLBACK: dict[int | str, dict[str, Any]] = {
    303: {"description": "Redirect to return_to (signed in), or to /login?error=<code>"}
}


def get_store(request: Request) -> OIDCProviderStore:
    store: OIDCProviderStore = request.app.state.oidc
    return store


StoreDep = Annotated[OIDCProviderStore, Depends(get_store)]


# -- Browser flow -----------------------------------------------------------------------


@router.get(
    "/{name}/login",
    status_code=status.HTTP_303_SEE_OTHER,
    response_class=RedirectResponse,
    responses=_REDIRECT,
)
async def oidc_login(
    name: str,
    request: Request,
    db: DbDep,
    settings: SettingsDep,
    store: StoreDep,
    return_to: Annotated[str | None, Query(max_length=2048)] = None,
) -> RedirectResponse:
    """Start the login with an OIDC provider (browser navigation, not fetch)."""
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
        error_type=OIDCError,
    )


@router.get(
    "/{name}/callback",
    status_code=status.HTTP_303_SEE_OTHER,
    response_class=RedirectResponse,
    responses=_CALLBACK,
)
async def oidc_callback(
    name: str, request: Request, db: DbDep, settings: SettingsDep, store: StoreDep
) -> RedirectResponse:
    """Redirect target of the IdP: validates the response and signs the user in."""
    provider = await store.enabled_provider(db, name)
    if provider is None:
        return redirect_flow.error_redirect(redirect_flow.FlowErrorCode.PROVIDER_UNKNOWN)
    return await redirect_flow.finish_login(
        request, db, settings, provider, provider.provisioning, error_type=OIDCError
    )


@router.post("/logout")
async def oidc_logout(
    request: Request, response: Response, db: DbDep, settings: SettingsDep, store: StoreDep
) -> LogoutResult:
    """End the current session like ``POST /auth/logout``. If it was started with an OIDC
    provider that supports RP-initiated logout, ``redirect_url`` ends the IdP session too
    (the browser navigates there and comes back to the login page)."""
    redirect_url = None
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        provider_name = await db.scalar(
            select(AuthSession.provider).where(AuthSession.token_hash == hash_token(token))
        )
        if provider_name and provider_name.startswith("oidc:"):
            provider = await store.enabled_provider(db, provider_name.removeprefix("oidc:"))
            if provider is not None:
                post_logout = (
                    redirect_flow.public_origin(settings, request) + redirect_flow.LOGIN_PAGE
                )
                redirect_url = await provider.logout_url(post_logout)
    await service.end_session(db, settings, request, response)
    return LogoutResult(redirect_url=redirect_url)


# -- Administration ---------------------------------------------------------------------


@admin_router.get("/presets")
async def list_oidc_presets(_: AdminSessionDep) -> list[OIDCPresetRead]:
    """Presets with defaults for the provider form (see docs/auth/oidc.md)."""
    return [
        OIDCPresetRead(
            preset=p.preset,
            label=p.label,
            issuer_template=p.issuer_template,
            scopes=list(p.scopes),
            groups_claim=p.groups_claim,
            fields=list(p.fields),
            docs=p.docs,
        )
        for p in PRESETS.values()
    ]


def _read(
    config: OIDCConfig,
    record: OIDCProviderRecord | None,
    request: Request,
    settings: SettingsDep,
) -> OIDCProviderRead:
    return OIDCProviderRead(
        name=config.name,
        provider=config.provider_name,
        source=config.source,
        display_name=config.display_name,
        preset=config.preset,
        issuer=config.issuer,
        client_id=config.client_id,
        has_client_secret=bool(config.client_secret),
        scopes=list(config.scopes),
        enabled=config.enabled,
        auto_provision=config.auto_provision,
        link_by_email=config.link_by_email,
        allowed_domains=sorted(config.allowed_domains),
        groups_claim=config.groups_claim,
        allowed_tenants=sorted(config.allowed_tenants),
        hosted_domains=sorted(config.hosted_domains),
        redirect_uri=redirect_flow.api_url(settings, request, f"/auth/oidc/{config.name}/callback"),
        created_at=record.created_at if record else None,
        updated_at=record.updated_at if record else None,
    )


async def _record(db: AsyncSession, name: str) -> OIDCProviderRecord | None:
    return await db.scalar(select(OIDCProviderRecord).where(OIDCProviderRecord.name == name))


async def _config_changed(db: AsyncSession, admin_id: Any, record_id: Any, change: str) -> None:
    await audit.record(
        db,
        audit.Actor.user(admin_id),
        audit.AuditAction.IDP_CONFIG_CHANGED,
        audit.Target.of(audit.TargetType.IDP, record_id),
        details={"kind": "oidc", "change": change},
    )


def _not_found() -> ProblemError:
    return ProblemError(404, detail="OIDC provider not found.")


def _read_only() -> ProblemError:
    return ProblemError(
        409,
        detail="This provider is configured in the environment and cannot be changed here.",
        type="urn:ollamail:problem:oidc-provider-read-only",
    )


def _validate(record: OIDCProviderRecord, store: OIDCProviderStore) -> None:
    try:
        validate_config(
            name=record.name,
            preset=record.preset,
            issuer=record.issuer,
            scopes=record.scopes,
            allowed_tenants=record.allowed_tenants,
            hosted_domains=record.hosted_domains,
            allow_http=store.allow_http,
        )
    except ValueError as exc:
        # Static reasons from the validators; they contain no secrets.
        raise ProblemError(
            422,
            detail=str(exc),
            type="urn:ollamail:problem:oidc-provider-invalid",
        ) from None


@admin_router.get("/providers")
async def list_oidc_providers(
    _: AdminSessionDep, request: Request, db: DbDep, settings: SettingsDep, store: StoreDep
) -> list[OIDCProviderRead]:
    """All OIDC providers (secrets are never returned)."""
    records = {r.name: r for r in await db.scalars(select(OIDCProviderRecord))}
    return [
        _read(c, records.get(c.name) if c.source == "db" else None, request, settings)
        for c in await store.configs(db)
    ]


@admin_router.get("/providers/{name}", responses={404: {"description": "Unknown provider"}})
async def get_oidc_provider(
    name: str,
    _: AdminSessionDep,
    request: Request,
    db: DbDep,
    settings: SettingsDep,
    store: StoreDep,
) -> OIDCProviderRead:
    config = await store.config(db, name)
    if config is None:
        raise _not_found()
    record = await _record(db, name) if config.source == "db" else None
    return _read(config, record, request, settings)


@admin_router.post(
    "/providers",
    status_code=status.HTTP_201_CREATED,
    responses={
        **ADMIN_REAUTH_RESPONSES,
        409: {"description": "Name taken"},
        422: {"description": "Invalid settings"},
    },
)
async def create_oidc_provider(
    body: OIDCProviderCreate,
    admin: RecentAdminDep,
    request: Request,
    db: DbDep,
    settings: SettingsDep,
    store: StoreDep,
) -> OIDCProviderRead:
    """Add an OIDC provider. The client secret is stored encrypted."""
    taken = ProblemError(
        409, detail="The name is already taken.", type="urn:ollamail:problem:name-taken"
    )
    if body.name in store.env_configs or await _record(db, body.name) is not None:
        raise taken
    record = OIDCProviderRecord(**body.model_dump())
    _validate(record, store)
    db.add(record)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise taken from None
    await _config_changed(db, admin.user_id, record.id, "created")
    await db.commit()
    await db.refresh(record)
    log.info("oidc_provider_created", provider_name=record.name, by_user_id=admin.user_id)
    return _read(await _config(store, db, record.name), record, request, settings)


async def _config(store: OIDCProviderStore, db: AsyncSession, name: str) -> OIDCConfig:
    config = await store.config(db, name)
    assert config is not None
    return config


# Nullable settings where an explicit null clears the value.
_CLEARABLE = frozenset({"client_secret", "groups_claim"})


@admin_router.patch(
    "/providers/{name}",
    responses={
        **ADMIN_REAUTH_RESPONSES,
        404: {"description": "Unknown provider"},
        409: {"description": "Configured in the environment, or admin lockout"},
        422: {"description": "Invalid settings"},
    },
)
async def update_oidc_provider(
    name: str,
    body: OIDCProviderUpdate,
    admin: RecentAdminDep,
    request: Request,
    db: DbDep,
    settings: SettingsDep,
    store: StoreDep,
) -> OIDCProviderRead:
    """Change an OIDC provider. Omitted fields stay as they are."""
    if name in store.env_configs:
        raise _read_only()
    record = await _record(db, name)
    if record is None:
        raise _not_found()
    guard = await AdminAccessGuard.start(db, request.app.state.auth_providers)
    for field in body.model_fields_set:
        value = getattr(body, field)
        if value is None and field not in _CLEARABLE:
            continue
        setattr(record, field, value)
    try:
        _validate(record, store)
    except ProblemError:
        await db.rollback()
        raise
    await guard.check()
    await _config_changed(db, admin.user_id, record.id, "updated")
    await db.commit()
    await db.refresh(record)
    log.info("oidc_provider_updated", provider_name=name, by_user_id=admin.user_id)
    return _read(await _config(store, db, name), record, request, settings)


@admin_router.delete(
    "/providers/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        404: {"description": "Unknown provider"},
        409: {"description": "Configured in the environment, or admin lockout"},
    },
)
async def delete_oidc_provider(
    name: str, admin: AdminSessionDep, request: Request, db: DbDep, store: StoreDep
) -> None:
    """Remove an OIDC provider. Users and their linked identities are kept; sessions
    started with the provider stay valid until they expire or are revoked."""
    if name in store.env_configs:
        raise _read_only()
    record = await _record(db, name)
    if record is None:
        raise _not_found()
    guard = await AdminAccessGuard.start(db, request.app.state.auth_providers)
    record_id = record.id
    await db.delete(record)
    await guard.check()
    await _config_changed(db, admin.user_id, record_id, "deleted")
    await db.commit()
    log.info("oidc_provider_deleted", provider_name=name, by_user_id=admin.user_id)


@admin_router.post("/providers/{name}/test", responses={404: {"description": "Unknown provider"}})
async def test_oidc_provider(
    name: str, _: AdminSessionDep, db: DbDep, store: StoreDep
) -> OIDCConnectionTest:
    """Fetch the discovery document and signing keys (bypassing the cache), as a login
    would. The client secret can only be checked by a real login."""
    config = await store.config(db, name)
    if config is None:
        raise _not_found()
    # A fresh cache: the test must reach the IdP, and must not replace cached keys.
    cache = MetadataCache(ttl=0, allow_http=store.allow_http, transport=store.transport)
    provider = OIDCProvider(config, cache)
    try:
        metadata = await provider.metadata()
        keys = await cache.keys(metadata)
    except OIDCError as exc:
        log.info("oidc_provider_test_failed", provider_name=name, reason=exc.reason)
        return OIDCConnectionTest(ok=False, error=exc.code.value)
    return OIDCConnectionTest(
        ok=True,
        issuer=metadata.issuer,
        authorization_endpoint=metadata.authorization_endpoint,
        token_endpoint=metadata.token_endpoint,
        end_session_supported=metadata.end_session_endpoint is not None,
        signing_keys=len(keys.keys),
    )
