"""SAML endpoints: browser login, Assertion Consumer Service, SP metadata and provider
administration (admin only)."""

from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import RedirectResponse, Response
from onelogin.saml2.settings import OneLogin_Saml2_Settings
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.auth import redirect_flow
from app.auth.admin_access import AdminAccessGuard
from app.auth.dependencies import AdminSessionDep, SettingsDep
from app.auth.providers.saml import store
from app.auth.providers.saml.config import (
    ACS_PATH,
    PROVIDER_PREFIX,
    certificate_info,
    from_record,
    metadata_path,
)
from app.auth.providers.saml.errors import SAMLError, SAMLMetadataError
from app.auth.providers.saml.metadata import IdPMetadata, fetch_metadata, parse_metadata
from app.auth.providers.saml.models import SAMLProviderRecord
from app.auth.providers.saml.presets import NAMEID_TRANSIENT, PRESETS
from app.auth.providers.saml.provider import SAMLProvider
from app.auth.providers.saml.schemas import (
    SAMLCertificateRead,
    SAMLProviderCreate,
    SAMLProviderRead,
    SAMLProviderUpdate,
)
from app.core.config import Settings
from app.core.db import get_db
from app.core.errors import ProblemError
from app.core.logging import get_logger

log = get_logger(__name__)

DbDep = Annotated[AsyncSession, Depends(get_db)]

router = APIRouter(prefix="/auth/saml", tags=["auth"])
admin_router = APIRouter(
    prefix="/admin/auth/saml",
    tags=["admin"],
    responses={401: {"description": "Not signed in"}, 403: {"description": "Not an admin"}},
)

_REDIRECT: dict[int | str, dict[str, Any]] = {
    303: {"description": "Redirect to the IdP, or to /login?error=<code>"}
}
_CALLBACK: dict[int | str, dict[str, Any]] = {
    303: {"description": "Redirect to return_to (signed in), or to /login?error=<code>"}
}
# The form the IdP posts: SAMLResponse (base64, up to 1 MiB) and RelayState.
_MAX_FORM_BYTES = 2 * 1024 * 1024
_INVALID = "urn:ollamail:problem:saml-provider-invalid"


def sp_entity_id(config_value: str | None, name: str, settings: Settings, request: Request) -> str:
    return config_value or redirect_flow.api_url(settings, request, metadata_path(name))


async def _bound_provider(
    db: AsyncSession, name: str, request: Request, settings: Settings
) -> SAMLProvider | None:
    record = await store.get_record(db, name)
    if record is None or not record.enabled:
        return None
    return SAMLProvider(
        from_record(record),
        sp_entity_id=sp_entity_id(record.sp_entity_id, record.name, settings, request),
        db=db,
    )


def _flow_samesite(settings: Settings) -> Literal["lax", "none"]:
    """The IdP posts the response cross-site; a ``Lax`` flow cookie would not be sent with
    it. ``None`` requires ``Secure``; without HTTPS (development) IdP and ollamail must
    share a site (e.g. both on localhost) for the login to work."""
    return "none" if settings.auth.cookie_secure else "lax"


# -- Browser flow -----------------------------------------------------------------------


@router.get(
    "/{name}/login",
    status_code=status.HTTP_303_SEE_OTHER,
    response_class=RedirectResponse,
    responses=_REDIRECT,
)
async def saml_login(
    name: str,
    request: Request,
    db: DbDep,
    settings: SettingsDep,
    return_to: Annotated[str | None, Query(max_length=2048)] = None,
) -> RedirectResponse:
    """Start the login with a SAML provider (browser navigation, not fetch)."""
    provider = await _bound_provider(db, name, request, settings)
    if provider is None:
        return redirect_flow.error_redirect(redirect_flow.FlowErrorCode.PROVIDER_UNKNOWN)
    return await redirect_flow.start_login(
        request,
        db,
        settings,
        provider,
        callback_path=ACS_PATH,
        return_to=return_to,
        error_type=SAMLError,
        samesite=_flow_samesite(settings),
    )


async def _read_form(request: Request) -> dict[str, str] | None:
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        return None
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > _MAX_FORM_BYTES:
            return None
    try:
        pairs = parse_qsl(body.decode("ascii"), keep_blank_values=True, max_num_fields=10)
    except (UnicodeDecodeError, ValueError):
        return None
    return dict(pairs)


@router.post(
    "/acs",
    status_code=status.HTTP_303_SEE_OTHER,
    response_class=RedirectResponse,
    responses=_CALLBACK,
    openapi_extra={
        "requestBody": {
            "content": {
                "application/x-www-form-urlencoded": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "SAMLResponse": {"type": "string"},
                            "RelayState": {"type": "string"},
                        },
                        "required": ["SAMLResponse", "RelayState"],
                    }
                }
            },
            "required": True,
        }
    },
)
async def saml_acs(request: Request, db: DbDep, settings: SettingsDep) -> RedirectResponse:
    """Assertion Consumer Service (HTTP-POST binding): the IdP posts the response here.

    The provider comes from the encrypted flow cookie, not from the request; responses
    without a matching login started here (IdP-initiated) are rejected.
    """
    flow = redirect_flow.unseal(settings, request.cookies.get(redirect_flow.FLOW_COOKIE))
    form = await _read_form(request)
    if flow is None or form is None or not flow.provider.startswith(PROVIDER_PREFIX):
        log.warning(
            "login_failed", provider="saml", reason=redirect_flow.FlowErrorCode.STATE_INVALID
        )
        response = redirect_flow.error_redirect(redirect_flow.FlowErrorCode.STATE_INVALID)
        redirect_flow.clear_flow_cookie(response, settings)
        return response
    provider = await _bound_provider(
        db, flow.provider.removeprefix(PROVIDER_PREFIX), request, settings
    )
    if provider is None:
        return redirect_flow.error_redirect(redirect_flow.FlowErrorCode.PROVIDER_UNKNOWN)
    return await redirect_flow.finish_login(
        request,
        db,
        settings,
        provider,
        provider.provisioning,
        error_type=SAMLError,
        params={
            "state": form.get("RelayState", ""),
            "SAMLResponse": form.get("SAMLResponse", ""),
        },
    )


@router.get(
    "/{name}/metadata",
    response_class=Response,
    responses={
        200: {"content": {"application/samlmetadata+xml": {}}, "description": "SP metadata"},
        404: {"description": "Unknown provider"},
    },
)
async def saml_sp_metadata(
    name: str, request: Request, db: DbDep, settings: SettingsDep
) -> Response:
    """Metadata of ollamail as service provider (entity ID, ACS URL) for the IdP; also
    for disabled providers, so it can be registered before the provider is switched on."""
    record = await store.get_record(db, name)
    if record is None:
        raise _not_found()
    config = from_record(record)
    library_settings = config.library_settings(
        sp_entity_id=sp_entity_id(record.sp_entity_id, record.name, settings, request),
        acs_url=redirect_flow.api_url(settings, request, ACS_PATH),
    )
    xml = OneLogin_Saml2_Settings(library_settings, sp_validation_only=True).get_sp_metadata()
    return Response(xml, media_type="application/samlmetadata+xml")


# -- Administration ---------------------------------------------------------------------


def _read(record: SAMLProviderRecord, request: Request, settings: Settings) -> SAMLProviderRead:
    certificates = [certificate_info(c) for c in record.idp_certificates]
    return SAMLProviderRead(
        name=record.name,
        provider=PROVIDER_PREFIX + record.name,
        display_name=record.display_name,
        preset=from_record(record).preset,
        metadata_url=record.metadata_url,
        metadata_refreshed_at=record.metadata_refreshed_at,
        idp_entity_id=record.idp_entity_id,
        idp_sso_url=record.idp_sso_url,
        idp_certificates=[
            SAMLCertificateRead(
                fingerprint_sha256=c.fingerprint_sha256, not_valid_after=c.not_valid_after
            )
            for c in certificates
        ],
        sp_entity_id=record.sp_entity_id,
        effective_sp_entity_id=sp_entity_id(record.sp_entity_id, record.name, settings, request),
        redirect_uri=redirect_flow.api_url(settings, request, ACS_PATH),
        sp_metadata_url=redirect_flow.api_url(settings, request, metadata_path(record.name)),
        name_id_format=record.name_id_format,
        subject_attribute=record.subject_attribute,
        email_attribute=record.email_attribute,
        display_name_attribute=record.display_name_attribute,
        groups_attribute=record.groups_attribute,
        trust_email=record.trust_email,
        enabled=record.enabled,
        auto_provision=record.auto_provision,
        link_by_email=record.link_by_email,
        allowed_domains=list(record.allowed_domains),
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


async def _config_changed(db: AsyncSession, admin_id: Any, record_id: Any, change: str) -> None:
    await audit.record(
        db,
        audit.Actor.user(admin_id),
        audit.AuditAction.IDP_CONFIG_CHANGED,
        audit.Target.of(audit.TargetType.IDP, record_id),
        details={"kind": "saml", "change": change},
    )


def _not_found() -> ProblemError:
    return ProblemError(404, detail="SAML provider not found.")


def _invalid(detail: str) -> ProblemError:
    return ProblemError(422, detail=detail, type=_INVALID)


def _apply_metadata(record: SAMLProviderRecord, metadata: IdPMetadata) -> None:
    record.idp_entity_id = metadata.entity_id
    record.idp_sso_url = metadata.sso_url
    record.idp_certificates = list(metadata.certificates)


async def _load_metadata(record: SAMLProviderRecord, xml: str | None, fetch: bool) -> None:
    """Replace the IdP settings from uploaded ``xml`` or, if ``fetch``, from the URL."""
    try:
        if xml is not None:
            _apply_metadata(record, parse_metadata(xml.encode()))
        elif fetch and record.metadata_url:
            _apply_metadata(record, await fetch_metadata(record.metadata_url))
            record.metadata_refreshed_at = datetime.now(UTC)
        else:
            return
    except SAMLMetadataError as exc:
        # Static message from app.auth.providers.saml.metadata; no document content.
        raise ProblemError(
            422, detail=str(exc), type="urn:ollamail:problem:saml-metadata-invalid"
        ) from None


def _check(record: SAMLProviderRecord) -> None:
    """Complete IdP settings and a subject that stays the same across logins."""
    if not (record.idp_entity_id and record.idp_sso_url and record.idp_certificates):
        raise _invalid(
            "The IdP entity ID, single sign-on URL and signing certificate are required; "
            "provide metadata_url, metadata_xml or the idp_* fields."
        )
    if record.subject_attribute is None and record.name_id_format == NAMEID_TRANSIENT:
        raise _invalid("A transient NameID changes at every login; set subject_attribute.")
    record.idp_certificates = list(dict.fromkeys(record.idp_certificates))
    record.allowed_domains = sorted(set(record.allowed_domains))


async def _record(db: AsyncSession, name: str) -> SAMLProviderRecord:
    record = await store.get_record(db, name)
    if record is None:
        raise _not_found()
    return record


@admin_router.get("/providers")
async def list_saml_providers(
    _: AdminSessionDep, request: Request, db: DbDep, settings: SettingsDep
) -> list[SAMLProviderRead]:
    """All SAML providers."""
    return [_read(r, request, settings) for r in await store.records(db)]


@admin_router.get("/providers/{name}", responses={404: {"description": "Unknown provider"}})
async def get_saml_provider(
    name: str, _: AdminSessionDep, request: Request, db: DbDep, settings: SettingsDep
) -> SAMLProviderRead:
    return _read(await _record(db, name), request, settings)


# Settings the preset fills in when the request leaves them out.
_PRESET_FIELDS = (
    "name_id_format",
    "subject_attribute",
    "email_attribute",
    "display_name_attribute",
    "groups_attribute",
)


@admin_router.post(
    "/providers",
    status_code=status.HTTP_201_CREATED,
    responses={
        409: {"description": "Name taken"},
        422: {"description": "Invalid settings or metadata"},
    },
)
async def create_saml_provider(
    body: SAMLProviderCreate,
    admin: AdminSessionDep,
    request: Request,
    db: DbDep,
    settings: SettingsDep,
) -> SAMLProviderRead:
    """Add a SAML provider from IdP metadata (URL or upload) or explicit IdP settings."""
    taken = ProblemError(
        409, detail="The name is already taken.", type="urn:ollamail:problem:name-taken"
    )
    if await store.get_record(db, body.name) is not None:
        raise taken
    preset = PRESETS[body.preset]
    values = body.model_dump(exclude={"metadata_xml"})
    for field in _PRESET_FIELDS:
        if field not in body.model_fields_set:
            values[field] = getattr(preset, field)
    values["idp_entity_id"] = values["idp_entity_id"] or ""
    values["idp_sso_url"] = values["idp_sso_url"] or ""
    record = SAMLProviderRecord(**values)
    explicit_idp = bool(body.idp_entity_id and body.idp_sso_url and body.idp_certificates)
    await _load_metadata(record, body.metadata_xml, fetch=not explicit_idp)
    _check(record)
    db.add(record)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise taken from None
    await _config_changed(db, admin.user_id, record.id, "created")
    await db.commit()
    await db.refresh(record)
    log.info("saml_provider_created", provider_name=record.name, by_user_id=admin.user_id)
    return _read(record, request, settings)


# Nullable settings where an explicit null clears the value.
_CLEARABLE = frozenset(
    {
        "metadata_url",
        "sp_entity_id",
        "subject_attribute",
        "email_attribute",
        "display_name_attribute",
        "groups_attribute",
    }
)
_IDP_FIELDS = frozenset({"idp_entity_id", "idp_sso_url", "idp_certificates"})


@admin_router.patch(
    "/providers/{name}",
    responses={
        404: {"description": "Unknown provider"},
        409: {"description": "No administrator could sign in afterwards (admin-lockout)"},
        422: {"description": "Invalid settings or metadata"},
    },
)
async def update_saml_provider(
    name: str,
    body: SAMLProviderUpdate,
    admin: AdminSessionDep,
    request: Request,
    db: DbDep,
    settings: SettingsDep,
) -> SAMLProviderRead:
    """Change a SAML provider. Omitted fields stay as they are."""
    guard = await AdminAccessGuard.start(db, request.app.state.auth_providers)
    record = await _record(db, name)
    old_url = record.metadata_url
    for field in body.model_fields_set - {"metadata_xml"}:
        value = getattr(body, field)
        if value is None and field not in _CLEARABLE:
            continue
        setattr(record, field, value)
    url_changed = record.metadata_url is not None and record.metadata_url != old_url
    try:
        await _load_metadata(
            record,
            body.metadata_xml,
            fetch=url_changed and not (body.model_fields_set & _IDP_FIELDS),
        )
        _check(record)
    except ProblemError:
        await db.rollback()
        raise
    await guard.check()
    await _config_changed(db, admin.user_id, record.id, "updated")
    await db.commit()
    await db.refresh(record)
    log.info("saml_provider_updated", provider_name=name, by_user_id=admin.user_id)
    return _read(record, request, settings)


@admin_router.post(
    "/providers/{name}/refresh-metadata",
    responses={
        404: {"description": "Unknown provider"},
        409: {"description": "No administrator could sign in afterwards (admin-lockout)"},
        422: {"description": "No metadata URL, or the metadata is unusable"},
    },
)
async def refresh_saml_metadata(
    name: str,
    admin: AdminSessionDep,
    request: Request,
    db: DbDep,
    settings: SettingsDep,
) -> SAMLProviderRead:
    """Load the IdP metadata from ``metadata_url`` again (e.g. after a certificate
    rollover at the IdP)."""
    guard = await AdminAccessGuard.start(db, request.app.state.auth_providers)
    record = await _record(db, name)
    if not record.metadata_url:
        raise _invalid("The provider has no metadata URL.")
    try:
        await _load_metadata(record, None, fetch=True)
        _check(record)
    except ProblemError:
        await db.rollback()
        raise
    await guard.check()
    await _config_changed(db, admin.user_id, record.id, "metadata_refreshed")
    await db.commit()
    await db.refresh(record)
    log.info("saml_metadata_refreshed", provider_name=name, by_user_id=admin.user_id)
    return _read(record, request, settings)


@admin_router.delete(
    "/providers/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        404: {"description": "Unknown provider"},
        409: {"description": "No administrator could sign in afterwards (admin-lockout)"},
    },
)
async def delete_saml_provider(
    name: str, admin: AdminSessionDep, request: Request, db: DbDep
) -> None:
    """Remove a SAML provider. Users and their linked identities are kept; sessions
    started with the provider stay valid until they expire or are revoked."""
    guard = await AdminAccessGuard.start(db, request.app.state.auth_providers)
    record = await _record(db, name)
    record_id = record.id
    await db.delete(record)
    await guard.check()
    await _config_changed(db, admin.user_id, record_id, "deleted")
    await db.commit()
    log.info("saml_provider_deleted", provider_name=name, by_user_id=admin.user_id)
