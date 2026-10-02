"""OAuth 2.0 for Microsoft Graph mailboxes (docs/providers/microsoft365.md §3, §4).

* Delegated: authorization code flow with PKCE and client secret (``authorization_url``,
  ``exchange_code``); ``DelegatedTokens`` refreshes the access token and stores rotated
  tokens through ``MailboxConfig.save_credentials``.
* App-only: client credentials (``AppTokens``); tokens are cached per process only.

Credentials of a delegated mailbox (``Mailbox.credentials``, encrypted)::

    {"refresh_token": "...", "access_token": "...", "expires_at": <unix seconds>}

Errors carry static codes only; token responses, codes and secrets are never logged.
"""

import asyncio
import base64
import hashlib
import secrets
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from app.core.config import GraphSettings
from app.core.logging import get_logger
from app.mail.providers.base import (
    AuthenticationError,
    ConfigurationError,
    ConnectionFailedError,
    SaveCredentials,
)

log = get_logger(__name__)

# Refresh this many seconds before the access token expires.
EXPIRY_MARGIN = 300
_SHARED_SCOPE = "Mail.ReadWrite.Shared"
# Authorities that do not name a single tenant (no client credentials possible).
MULTI_TENANT_AUTHORITIES = frozenset({"organizations", "common", "consumers"})
_DELEGATED_SCOPES = ("User.Read", "Mail.ReadWrite")

Clock = Callable[[], float]
# Returns the access token; ``True`` forces a refresh (after a 401).
TokenGetter = Callable[[bool], Awaitable[str]]


def resource(settings: GraphSettings) -> str:
    """Graph resource (scope prefix), e.g. ``https://graph.microsoft.com``."""
    url = httpx.URL(settings.api_url)
    return f"{url.scheme}://{url.netloc.decode('ascii')}"


def delegated_scopes(settings: GraphSettings, *, shared: bool = False) -> list[str]:
    names = [*_DELEGATED_SCOPES, *([_SHARED_SCOPE] if shared else [])]
    return ["offline_access", *(f"{resource(settings)}/{name}" for name in names)]


def app_scopes(settings: GraphSettings) -> list[str]:
    return [f"{resource(settings)}/.default"]


def _endpoint(settings: GraphSettings, tenant: str, path: str) -> str:
    return f"{settings.authority}/{tenant}/oauth2/v2.0/{path}"


def require_client(settings: GraphSettings) -> tuple[str, str]:
    if not settings.client_id or settings.client_secret is None:
        raise ConfigurationError(code="graph_not_configured")
    return settings.client_id, settings.client_secret.get_secret_value()


# -- PKCE ---------------------------------------------------------------------------------


def code_verifier() -> str:
    # 64 characters of the unreserved set (RFC 7636 §4.1).
    return secrets.token_urlsafe(48)


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def authorization_url(
    settings: GraphSettings,
    *,
    redirect_uri: str,
    state: str,
    verifier: str,
    scopes: Sequence[str],
    login_hint: str | None = None,
) -> str:
    client_id, _ = require_client(settings)
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": code_challenge(verifier),
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    if login_hint:
        params["login_hint"] = login_hint
    return f"{_endpoint(settings, settings.tenant_id, 'authorize')}?{urlencode(params)}"


# -- token endpoint -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TokenSet:
    access_token: str
    expires_at: int
    refresh_token: str | None = None

    def __repr__(self) -> str:
        return f"TokenSet(expires_at={self.expires_at})"

    def valid(self, now: float) -> bool:
        return bool(self.access_token) and now < self.expires_at - EXPIRY_MARGIN

    def to_credentials(self) -> dict[str, Any]:
        data: dict[str, Any] = {"access_token": self.access_token, "expires_at": self.expires_at}
        if self.refresh_token:
            data["refresh_token"] = self.refresh_token
        return data

    @classmethod
    def from_credentials(cls, credentials: dict[str, Any]) -> "TokenSet":
        try:
            expires_at = int(credentials.get("expires_at") or 0)
        except (TypeError, ValueError):
            expires_at = 0
        refresh = credentials.get("refresh_token")
        return cls(
            access_token=str(credentials.get("access_token") or ""),
            expires_at=expires_at,
            refresh_token=str(refresh) if refresh else None,
        )


async def _token_request(
    http: httpx.AsyncClient,
    settings: GraphSettings,
    tenant: str,
    data: dict[str, str],
    now: float,
) -> TokenSet:
    client_id, client_secret = require_client(settings)
    form = {"client_id": client_id, "client_secret": client_secret, **data}
    try:
        response = await http.post(_endpoint(settings, tenant, "token"), data=form)
    except httpx.TransportError as exc:
        log.warning("graph_token_request_failed", error_type=type(exc).__name__)
        raise ConnectionFailedError() from None
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        body = {}
    if response.status_code >= 500 or response.status_code == 429:
        raise ConnectionFailedError(code="token_endpoint_unavailable")
    if not response.is_success or not isinstance(body.get("access_token"), str):
        # invalid_grant (revoked/expired refresh token), invalid_client (secret expired),
        # consent_required, ... The user or admin has to act; retrying does not help.
        error = body.get("error") if isinstance(body.get("error"), str) else "unknown"
        log.warning("graph_token_rejected", error=str(error)[:64])
        raise AuthenticationError()
    try:
        lifetime = int(body.get("expires_in", 3600))
    except (TypeError, ValueError):
        lifetime = 3600
    refresh = body.get("refresh_token")
    return TokenSet(
        access_token=body["access_token"],
        expires_at=int(now) + lifetime,
        refresh_token=refresh if isinstance(refresh, str) else None,
    )


async def exchange_code(
    http: httpx.AsyncClient,
    settings: GraphSettings,
    *,
    code: str,
    redirect_uri: str,
    verifier: str,
    scopes: Sequence[str],
    now: float | None = None,
) -> TokenSet:
    return await _token_request(
        http,
        settings,
        settings.tenant_id,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
            "scope": " ".join(scopes),
        },
        time.time() if now is None else now,
    )


class DelegatedTokens:
    """Access token of a delegated mailbox; refreshes and saves rotated tokens."""

    def __init__(
        self,
        settings: GraphSettings,
        http: httpx.AsyncClient,
        credentials: dict[str, Any],
        *,
        tenant: str | None = None,
        shared: bool = False,
        save: SaveCredentials | None = None,
        clock: Clock = time.time,
    ) -> None:
        self._settings = settings
        self._http = http
        self._tokens = TokenSet.from_credentials(credentials)
        self._tenant = tenant or settings.tenant_id
        self._scopes = delegated_scopes(settings, shared=shared)
        self._save = save
        self._clock = clock
        self._lock = asyncio.Lock()

    async def __call__(self, force: bool = False) -> str:
        async with self._lock:
            if force or not self._tokens.valid(self._clock()):
                await self._refresh()
            return self._tokens.access_token

    async def _refresh(self) -> None:
        if not self._tokens.refresh_token:
            raise AuthenticationError()
        tokens = await _token_request(
            self._http,
            self._settings,
            self._tenant,
            {
                "grant_type": "refresh_token",
                "refresh_token": self._tokens.refresh_token,
                "scope": " ".join(self._scopes),
            },
            self._clock(),
        )
        if tokens.refresh_token is None:
            # Not rotated: keep the previous refresh token.
            tokens = TokenSet(tokens.access_token, tokens.expires_at, self._tokens.refresh_token)
        self._tokens = tokens
        if self._save is not None:
            await self._save(tokens.to_credentials())
        log.info("graph_token_refreshed")


# App-only tokens per (authority, tenant, client ID), shared by the providers of a process.
_app_tokens: dict[tuple[str, str, str], TokenSet] = {}


def clear_app_token_cache() -> None:
    _app_tokens.clear()


class AppTokens:
    """Client-credentials token (app-only); cached in memory, never stored."""

    def __init__(
        self,
        settings: GraphSettings,
        http: httpx.AsyncClient,
        *,
        tenant: str | None = None,
        clock: Clock = time.time,
    ) -> None:
        tenant = tenant or settings.tenant_id
        if tenant in MULTI_TENANT_AUTHORITIES:
            # Client credentials need the tenant that granted the admin consent.
            raise ConfigurationError(code="graph_tenant_required")
        self._settings = settings
        self._http = http
        self._tenant = tenant
        self._clock = clock
        self._lock = asyncio.Lock()

    @property
    def _key(self) -> tuple[str, str, str]:
        return (self._settings.authority, self._tenant, self._settings.client_id or "")

    async def __call__(self, force: bool = False) -> str:
        async with self._lock:
            cached = _app_tokens.get(self._key)
            if force or cached is None or not cached.valid(self._clock()):
                cached = await _token_request(
                    self._http,
                    self._settings,
                    self._tenant,
                    {
                        "grant_type": "client_credentials",
                        "scope": " ".join(app_scopes(self._settings)),
                    },
                    self._clock(),
                )
                _app_tokens[self._key] = cached
            return cached.access_token
