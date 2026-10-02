"""Browser flow for redirect providers (OIDC #30, GitHub #31, SAML #94).

``start_login`` creates ``state``, ``nonce`` and a PKCE ``code_verifier``, stores them in
a short-lived, encrypted and authenticated cookie (AES-256-GCM, key derived from
``OLLAMAIL_SECRET_KEY``) and redirects to the IdP. ``finish_login`` handles the callback:
the ``state`` parameter must match the cookie (no login CSRF: an attacker cannot make a
victim complete the attacker's login), the provider validates code and ID token with the
nonce and verifier from the cookie, ``app.auth.provisioning`` maps the identity to a user,
and a normal server-side session starts. The cookie is single-use and deleted afterwards.

Nothing is stored server-side, so any API instance can handle the callback. Both
endpoints are browser navigations: errors redirect to the login page with a static code
(``/login?error=<code>``) instead of returning JSON.
"""

import base64
import binascii
import hmac
import json
import os
import re
import secrets
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Literal

from authlib.common.security import generate_token
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.auth import service
from app.auth.keys import derive_key
from app.auth.providers.base import RedirectAuthProvider
from app.auth.provisioning import ProvisioningError, ProvisioningPolicy, provision_user
from app.core.config import Settings
from app.core.errors import ProblemError
from app.core.logging import get_logger

log = get_logger(__name__)

FLOW_COOKIE = "ollamail_login_flow"
FLOW_LIFETIME_SECONDS = 600
# The reverse proxy (Caddy, Vite) serves the API below this prefix.
API_PREFIX = "/api"
LOGIN_PAGE = "/login"
_AAD = b"ollamail login flow v1"
_NONCE_LEN = 12
_MAX_RETURN_TO = 2048
# Unreserved characters only (RFC 7636 §4.1), 64 characters ≈ 380 bits.
_CODE_VERIFIER_LENGTH = 64
_SAFE_ERROR = re.compile(r"^[a-z0-9_]{1,64}$")


class FlowErrorCode:
    STATE_INVALID = "state_invalid"
    TOO_MANY_ATTEMPTS = "too_many_attempts"
    PROVIDER_UNKNOWN = "provider_unknown"


@dataclass(frozen=True)
class LoginFlow:
    provider: str
    state: str
    nonce: str
    code_verifier: str
    redirect_uri: str
    return_to: str
    expires_at: int


def _key(settings: Settings) -> bytes:
    return derive_key(settings.security, "login-flow")


def seal(settings: Settings, flow: LoginFlow) -> str:
    nonce = os.urandom(_NONCE_LEN)
    data = AESGCM(_key(settings)).encrypt(nonce, json.dumps(asdict(flow)).encode(), _AAD)
    return base64.urlsafe_b64encode(nonce + data).rstrip(b"=").decode()


def unseal(settings: Settings, value: str | None) -> LoginFlow | None:
    """The flow from the cookie, or ``None`` if missing, tampered with or expired."""
    if not value:
        return None
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        plain = AESGCM(_key(settings)).decrypt(raw[:_NONCE_LEN], raw[_NONCE_LEN:], _AAD)
        flow = LoginFlow(**json.loads(plain))
    except (InvalidTag, binascii.Error, ValueError, TypeError):
        return None
    if flow.expires_at < time.time():
        return None
    return flow


def safe_return_to(value: str | None) -> str:
    """A path on this site to return to after login; anything else becomes ``/``.

    Rejects absolute and protocol-relative URLs (``//evil``, ``/\\evil``) to prevent
    open redirects.
    """
    if (
        not value
        or len(value) > _MAX_RETURN_TO
        or not value.startswith("/")
        or value.startswith(("//", "/\\"))
        or any(ord(c) < 0x20 or c == "\\" for c in value)
    ):
        return "/"
    return value


def public_origin(settings: Settings, request: Request) -> str:
    """``OLLAMAIL_AUTH_PUBLIC_URL`` or the origin of the request."""
    if settings.auth.public_url:
        return settings.auth.public_url.rstrip("/")
    return f"{request.url.scheme}://{request.url.netloc}"


def api_url(settings: Settings, request: Request, path: str) -> str:
    return public_origin(settings, request) + API_PREFIX + path


def error_redirect(code: str) -> RedirectResponse:
    return RedirectResponse(f"{LOGIN_PAGE}?error={code}", status_code=303)


def set_flow_cookie(
    response: RedirectResponse,
    settings: Settings,
    flow: LoginFlow,
    samesite: Literal["lax", "none"] = "lax",
) -> None:
    response.set_cookie(
        FLOW_COOKIE,
        seal(settings, flow),
        max_age=FLOW_LIFETIME_SECONDS,
        path="/",
        secure=settings.auth.cookie_secure,
        httponly=True,
        # Lax: the callback is a top-level GET navigation coming from the IdP. SAML posts
        # its response cross-site and needs None (with Secure).
        samesite=samesite,
    )


def clear_flow_cookie(response: RedirectResponse, settings: Settings) -> None:
    response.delete_cookie(
        FLOW_COOKIE, path="/", secure=settings.auth.cookie_secure, httponly=True, samesite="lax"
    )


async def start_login(
    request: Request,
    db: AsyncSession,
    settings: Settings,
    provider: RedirectAuthProvider,
    *,
    callback_path: str,
    return_to: str | None,
    error_type: type[Exception],
    samesite: Literal["lax", "none"] = "lax",
) -> RedirectResponse:
    """Redirect the browser to the IdP and remember the flow in the cookie.

    ``error_type`` is the provider's error (e.g. IdP unreachable); it must carry a static
    ``code``. ``samesite`` is the flow cookie's attribute (``none`` for IdPs that post
    the response cross-site, like SAML).
    """
    try:
        await service.throttle_ip(db, settings, request)
    except ProblemError:
        log.warning("login_rejected", reason=FlowErrorCode.TOO_MANY_ATTEMPTS)
        return error_redirect(FlowErrorCode.TOO_MANY_ATTEMPTS)
    flow = LoginFlow(
        provider=provider.name,
        state=secrets.token_urlsafe(32),
        nonce=secrets.token_urlsafe(32),
        code_verifier=generate_token(_CODE_VERIFIER_LENGTH),
        redirect_uri=api_url(settings, request, callback_path),
        return_to=safe_return_to(return_to),
        expires_at=int(time.time()) + FLOW_LIFETIME_SECONDS,
    )
    try:
        url = await provider.authorization_url(
            state=flow.state,
            nonce=flow.nonce,
            redirect_uri=flow.redirect_uri,
            code_verifier=flow.code_verifier,
        )
    except error_type as exc:
        code = str(getattr(exc, "code", "provider_unavailable"))
        log.warning("login_failed", provider=provider.name, reason=code)
        return error_redirect(code)
    response = RedirectResponse(url, status_code=303)
    set_flow_cookie(response, settings, flow, samesite)
    return response


async def _login_failed(db: AsyncSession, provider: str, reason: str) -> None:
    await audit.record(
        db,
        audit.ANONYMOUS,
        audit.AuditAction.LOGIN_FAILED,
        details={"provider": provider, "reason": reason},
    )
    await db.commit()


def idp_error_code(value: str | None) -> str:
    """The IdP's ``error`` parameter if it looks like an OAuth error code, else ``other``."""
    return value if value and _SAFE_ERROR.match(value) else "other"


async def finish_login(
    request: Request,
    db: AsyncSession,
    settings: Settings,
    provider: RedirectAuthProvider,
    policy: ProvisioningPolicy,
    *,
    error_type: type[Exception],
    params: Mapping[str, str] | None = None,
) -> RedirectResponse:
    """Validate the callback, provision the user and start the session.

    ``error_type`` is the provider's validation error; it must carry a static ``code``.
    ``params`` are the callback parameters including ``state`` (default: the query
    string; SAML passes its posted form).
    Failures after a valid ``state`` (a real round trip to the IdP) are audited; callbacks
    without a matching flow cookie are only logged, since anyone can send them.
    """
    params = dict(request.query_params) if params is None else dict(params)
    flow = unseal(settings, request.cookies.get(FLOW_COOKIE))
    state = params.get("state", "")
    if (
        flow is None
        or flow.provider != provider.name
        or not hmac.compare_digest(state.encode(), flow.state.encode())
    ):
        log.warning("login_failed", provider=provider.name, reason=FlowErrorCode.STATE_INVALID)
        response = error_redirect(FlowErrorCode.STATE_INVALID)
        clear_flow_cookie(response, settings)
        return response

    try:
        identity = await provider.complete(
            params=params,
            nonce=flow.nonce,
            redirect_uri=flow.redirect_uri,
            code_verifier=flow.code_verifier,
        )
        result = await provision_user(db, identity, policy)
    except ProvisioningError as exc:
        await db.rollback()
        log.info("login_failed", provider=provider.name, reason=exc.code)
        await _login_failed(db, provider.name, exc.code)
        response = error_redirect(exc.code)
        clear_flow_cookie(response, settings)
        return response
    except error_type as exc:
        code = str(getattr(exc, "code", "invalid_response"))
        log.warning(
            "login_failed",
            provider=provider.name,
            reason=code,
            check=getattr(exc, "reason", None),
            idp_error=idp_error_code(params.get("error")) if "error" in params else None,
        )
        await _login_failed(db, provider.name, code)
        response = error_redirect(code)
        clear_flow_cookie(response, settings)
        return response

    response = RedirectResponse(flow.return_to, status_code=303)
    await audit.record(
        db,
        audit.Actor.user(result.user.id),
        audit.AuditAction.LOGIN_SUCCEEDED,
        details={"provider": provider.name},
    )
    await service.start_session(
        db, settings, request, response, result.user, provider=provider.name
    )
    clear_flow_cookie(response, settings)
    log.info("login_succeeded", user_id=result.user.id, provider=provider.name)
    return response
