"""Google OAuth 2.0 tokens for the Gmail provider (docs/providers/gmail.md §2).

* ``RefreshTokenSource``: per-user OAuth; the refresh token is stored encrypted in
  ``Mailbox.credentials``, access tokens are kept in memory only.
* ``ServiceAccountTokenSource``: Workspace domain-wide delegation (``subject`` = mailbox
  address) and Pub/Sub pull (no subject). The JWT is signed locally (RS256); no Google SDK.
* Building blocks of the connect flow: ``pkce_pair``, ``authorization_url`` and
  ``exchange_code``.

Access tokens are cached per process and refreshed shortly before they expire, so a
provider created for every sync does not hit the token endpoint each time. Tokens, keys and
Google's error texts never appear in exceptions or logs; only error codes do.
"""

import asyncio
import base64
import hashlib
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.core.config import GmailSettings
from app.mail.providers.base import AuthenticationError, ConfigurationError, ConnectionFailedError

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE_MODIFY = "https://www.googleapis.com/auth/gmail.modify"
SCOPE_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
SCOPE_PUBSUB = "https://www.googleapis.com/auth/pubsub"
# Google Tasks (todo export, app/todos/export/gtasks.py); requested by its own connect flow.
SCOPE_TASKS = "https://www.googleapis.com/auth/tasks"
JWT_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"
# Refresh this many seconds before the access token expires.
REFRESH_MARGIN = 120.0
_JWT_LIFETIME = 3600


def gmail_scope(settings: GmailSettings) -> str:
    return SCOPE_READONLY if settings.readonly else SCOPE_MODIFY


class TokenSource(Protocol):
    async def token(self, *, refresh: bool = False) -> str:
        """A valid access token; ``refresh`` forces a new one (after a 401)."""
        ...


@dataclass(slots=True)
class _CachedToken:
    value: str = field(repr=False)
    expires_at: float


_cache: dict[str, _CachedToken] = {}
_locks: dict[str, asyncio.Lock] = {}


def clear_token_cache() -> None:
    _cache.clear()
    _locks.clear()


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()


class _CachedSource:
    """Caches the access token under ``key`` (a digest, never the secret itself)."""

    def __init__(self, key: str, *, clock: Callable[[], float] = time.time) -> None:
        self._cache_key = key
        self._clock = clock

    async def token(self, *, refresh: bool = False) -> str:
        lock = _locks.setdefault(self._cache_key, asyncio.Lock())
        async with lock:
            cached = _cache.get(self._cache_key)
            if cached is not None and not refresh and cached.expires_at > self._clock():
                return cached.value
            value, expires_in = await self._fetch()
            _cache[self._cache_key] = _CachedToken(
                value, self._clock() + max(expires_in - REFRESH_MARGIN, 0.0)
            )
            return value

    async def _fetch(self) -> tuple[str, float]:
        raise NotImplementedError


async def _token_request(http: httpx.AsyncClient, url: str, data: dict[str, str]) -> dict[str, Any]:
    """POST to a token endpoint. 400/401/403 mean the grant was refused."""
    try:
        response = await http.post(url, data=data, headers={"Accept": "application/json"})
    except httpx.HTTPError:
        raise ConnectionFailedError() from None
    if response.status_code in (400, 401, 403):
        error = _json(response).get("error")
        if error == "invalid_grant":
            raise AuthenticationError(code="token_revoked")
        if error == "unauthorized_client":
            # Service account: domain-wide delegation not granted for this scope.
            raise AuthenticationError(code="delegation_denied")
        raise AuthenticationError()
    if response.status_code >= 400:
        raise ConnectionFailedError(code="token_endpoint_unavailable")
    payload = _json(response)
    if not isinstance(payload.get("access_token"), str):
        raise AuthenticationError()
    return payload


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _expires_in(payload: dict[str, Any]) -> float:
    try:
        return float(payload.get("expires_in", 3600))
    except (TypeError, ValueError):
        return 3600.0


class RefreshTokenSource(_CachedSource):
    """Access tokens from a user's refresh token (OAuth client of the instance)."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        settings: GmailSettings,
        refresh_token: str,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not settings.client_id or settings.client_secret is None:
            raise ConfigurationError(code="oauth_not_configured")
        super().__init__(_digest("refresh", settings.client_id, refresh_token), clock=clock)
        self._http = http
        self._settings = settings
        self._refresh_token = refresh_token

    async def _fetch(self) -> tuple[str, float]:
        assert self._settings.client_id and self._settings.client_secret
        payload = await _token_request(
            self._http,
            TOKEN_URL,
            {
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
                "client_id": self._settings.client_id,
                "client_secret": self._settings.client_secret.get_secret_value(),
            },
        )
        return payload["access_token"], _expires_in(payload)


@dataclass(frozen=True, slots=True)
class ServiceAccountKey:
    client_email: str
    private_key: str = field(repr=False)
    token_uri: str = TOKEN_URL


def load_service_account_key(path: Path) -> ServiceAccountKey:
    """Read a service account key file (JSON as downloaded from GCP)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        key = ServiceAccountKey(
            client_email=str(data["client_email"]),
            private_key=str(data["private_key"]),
            token_uri=str(data.get("token_uri") or TOKEN_URL),
        )
    except (OSError, ValueError, KeyError, TypeError):
        raise ConfigurationError(code="service_account_invalid") from None
    if not key.token_uri.startswith("https://"):
        raise ConfigurationError(code="service_account_invalid")
    return key


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def sign_jwt(key: ServiceAccountKey, scope: str, subject: str | None, now: float) -> str:
    """JWT assertion for the OAuth 2.0 JWT bearer grant (RFC 7523), signed with RS256."""
    try:
        private_key = serialization.load_pem_private_key(key.private_key.encode(), None)
    except (ValueError, TypeError):
        raise ConfigurationError(code="service_account_invalid") from None
    if not isinstance(private_key, rsa.RSAPrivateKey):
        raise ConfigurationError(code="service_account_invalid")
    claims: dict[str, Any] = {
        "iss": key.client_email,
        "scope": scope,
        "aud": key.token_uri,
        "iat": int(now),
        "exp": int(now) + _JWT_LIFETIME,
    }
    if subject:
        claims["sub"] = subject
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    body = _b64url(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{header}.{body}".encode("ascii")
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{body}.{_b64url(signature)}"


class ServiceAccountTokenSource(_CachedSource):
    """Access tokens of a service account, impersonating ``subject`` if given."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        key: ServiceAccountKey,
        scope: str,
        subject: str | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__(
            _digest("service_account", key.client_email, scope, (subject or "").lower()),
            clock=clock,
        )
        self._http = http
        self._key = key
        self._scope = scope
        self._subject = subject

    async def _fetch(self) -> tuple[str, float]:
        assertion = sign_jwt(self._key, self._scope, self._subject, self._clock())
        payload = await _token_request(
            self._http, self._key.token_uri, {"grant_type": JWT_GRANT, "assertion": assertion}
        )
        return payload["access_token"], _expires_in(payload)


# --- connect flow -----------------------------------------------------------------------


def pkce_pair() -> tuple[str, str]:
    """PKCE ``code_verifier`` and its S256 ``code_challenge`` (RFC 7636)."""
    verifier = secrets.token_urlsafe(48)
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def oauth_configured(settings: GmailSettings) -> bool:
    return bool(settings.client_id and settings.client_secret and settings.redirect_uri)


def authorization_url(
    settings: GmailSettings,
    *,
    state: str,
    code_challenge: str,
    login_hint: str | None = None,
    scope: str | None = None,
    redirect_uri: str | None = None,
) -> str:
    """Google's consent URL; ``scope`` and ``redirect_uri`` default to the Gmail connect
    flow. Other flows of the same OAuth client (Google Tasks) pass their own and keep the
    scopes granted before (``include_granted_scopes``)."""
    if not oauth_configured(settings):
        raise ConfigurationError(code="oauth_not_configured")
    params = {
        "client_id": settings.client_id,
        "redirect_uri": redirect_uri or settings.redirect_uri,
        "response_type": "code",
        "scope": scope or gmail_scope(settings),
        # A refresh token is only issued with offline access; ``consent`` makes Google
        # issue a new one on reconnect, too.
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if scope:
        params["include_granted_scopes"] = "true"
    if login_hint:
        params["login_hint"] = login_hint
    return f"{AUTH_URL}?{urlencode(params)}"


@dataclass(frozen=True, slots=True)
class TokenGrant:
    access_token: str = field(repr=False)
    refresh_token: str | None = field(repr=False)
    scopes: frozenset[str]


async def exchange_code(
    http: httpx.AsyncClient,
    settings: GmailSettings,
    code: str,
    code_verifier: str,
    *,
    redirect_uri: str | None = None,
) -> TokenGrant:
    """Exchange an authorization code for tokens (``redirect_uri`` as in the consent URL)."""
    if not oauth_configured(settings):
        raise ConfigurationError(code="oauth_not_configured")
    assert settings.client_id and settings.client_secret and settings.redirect_uri
    payload = await _token_request(
        http,
        TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": code_verifier,
            "client_id": settings.client_id,
            "client_secret": settings.client_secret.get_secret_value(),
            "redirect_uri": redirect_uri or settings.redirect_uri,
        },
    )
    refresh_token = payload.get("refresh_token")
    scope = payload.get("scope")
    return TokenGrant(
        access_token=payload["access_token"],
        refresh_token=refresh_token if isinstance(refresh_token, str) else None,
        scopes=frozenset(scope.split()) if isinstance(scope, str) else frozenset(),
    )


def scope_granted(settings: GmailSettings, scopes: frozenset[str]) -> bool:
    """Google lets users untick scopes on the consent screen; check what was granted."""
    if settings.readonly:
        return SCOPE_READONLY in scopes or SCOPE_MODIFY in scopes
    return SCOPE_MODIFY in scopes
