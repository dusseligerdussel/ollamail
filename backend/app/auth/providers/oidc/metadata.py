"""OIDC discovery and signing keys (JWKS), cached per issuer.

The discovery document is fetched from ``<issuer>/.well-known/openid-configuration``; its
``issuer`` must equal the configured issuer (OpenID Connect Discovery §4.3), so a
compromised or mistyped URL cannot hand out another IdP's endpoints. All endpoints must use
TLS. Signing keys are re-fetched when a token names an unknown key ID (key rotation), at
most once per ``_JWKS_REFRESH_INTERVAL`` so forged key IDs cannot flood the IdP.
"""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from joserfc.errors import JoseError
from joserfc.jwk import KeySet

from app.auth.providers.oidc.errors import OIDCError, OIDCErrorCode

_TIMEOUT = httpx.Timeout(10.0)
_MAX_DOCUMENT_BYTES = 512 * 1024
_JWKS_REFRESH_INTERVAL = 60.0
# Asymmetric algorithms only: never "none", and no HMAC (would make the client secret a
# signing key).
SAFE_ALGORITHMS = (
    "RS256",
    "RS384",
    "RS512",
    "PS256",
    "PS384",
    "PS512",
    "ES256",
    "ES384",
    "ES512",
    "EdDSA",
)

TransportFactory = Callable[[], httpx.AsyncBaseTransport | None]


@dataclass(frozen=True)
class ProviderMetadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    userinfo_endpoint: str | None
    end_session_endpoint: str | None
    token_endpoint_auth_methods: tuple[str, ...]
    signing_algorithms: tuple[str, ...]


@dataclass
class _Cached[T]:
    value: T
    fetched_at: float


def _check_url(url: object, *, allow_http: bool) -> str:
    if not isinstance(url, str):
        raise OIDCError(OIDCErrorCode.DISCOVERY_FAILED)
    parts = urlsplit(url)
    if parts.scheme not in ({"https", "http"} if allow_http else {"https"}) or not parts.netloc:
        raise OIDCError(OIDCErrorCode.DISCOVERY_FAILED)
    return url


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(v for v in value if isinstance(v, str))


def parse_metadata(
    document: dict[str, Any], *, expected_issuer: str, allow_http: bool
) -> ProviderMetadata:
    if document.get("issuer") != expected_issuer:
        raise OIDCError(OIDCErrorCode.DISCOVERY_FAILED)

    def optional(key: str) -> str | None:
        value = document.get(key)
        return None if value is None else _check_url(value, allow_http=allow_http)

    algorithms = tuple(
        a
        for a in _strings(document.get("id_token_signing_alg_values_supported"))
        if a in SAFE_ALGORITHMS
    )
    return ProviderMetadata(
        issuer=expected_issuer,
        authorization_endpoint=_check_url(
            document.get("authorization_endpoint"), allow_http=allow_http
        ),
        token_endpoint=_check_url(document.get("token_endpoint"), allow_http=allow_http),
        jwks_uri=_check_url(document.get("jwks_uri"), allow_http=allow_http),
        userinfo_endpoint=optional("userinfo_endpoint"),
        end_session_endpoint=optional("end_session_endpoint"),
        token_endpoint_auth_methods=_strings(document.get("token_endpoint_auth_methods_supported")),
        # RS256 is mandatory for OPs (OpenID Connect Core §15.1).
        signing_algorithms=algorithms or ("RS256",),
    )


class MetadataCache:
    """Discovery documents and key sets of all configured issuers."""

    def __init__(
        self, *, ttl: float, allow_http: bool, transport: TransportFactory = lambda: None
    ) -> None:
        self.ttl = ttl
        self.allow_http = allow_http
        self.transport = transport
        self._metadata: dict[tuple[str, str], _Cached[ProviderMetadata]] = {}
        self._keys: dict[str, _Cached[KeySet]] = {}
        self._last_refresh: dict[str, float] = {}
        self._lock = asyncio.Lock()

    def client(self) -> httpx.AsyncClient:
        """HTTP client for requests to the IdP (no redirects, bounded timeouts)."""
        return httpx.AsyncClient(
            timeout=_TIMEOUT, follow_redirects=False, transport=self.transport()
        )

    def _fresh(self, entry: _Cached[Any] | None) -> bool:
        return entry is not None and time.monotonic() - entry.fetched_at < self.ttl

    async def _get_json(self, url: str) -> dict[str, Any]:
        try:
            async with self.client() as http:
                response = await http.get(url, headers={"Accept": "application/json"})
        except httpx.HTTPError:
            raise OIDCError(OIDCErrorCode.DISCOVERY_FAILED) from None
        if response.status_code != 200 or len(response.content) > _MAX_DOCUMENT_BYTES:
            raise OIDCError(OIDCErrorCode.DISCOVERY_FAILED)
        try:
            document = response.json()
        except ValueError:
            raise OIDCError(OIDCErrorCode.DISCOVERY_FAILED) from None
        if not isinstance(document, dict):
            raise OIDCError(OIDCErrorCode.DISCOVERY_FAILED)
        return document

    async def metadata(
        self, issuer: str, *, expected_issuer: str | None = None
    ) -> ProviderMetadata:
        """Discovery for ``issuer``. ``expected_issuer`` overrides the value the document must
        contain (Entra multi-tenant: ``.../{tenantid}/v2.0``)."""
        expected = expected_issuer or issuer
        key = (issuer, expected)
        cached = self._metadata.get(key)
        if cached is not None and self._fresh(cached):
            return cached.value
        url = issuer.rstrip("/") + "/.well-known/openid-configuration"
        document = await self._get_json(_check_url(url, allow_http=self.allow_http))
        metadata = parse_metadata(document, expected_issuer=expected, allow_http=self.allow_http)
        self._metadata[key] = _Cached(metadata, time.monotonic())
        return metadata

    async def keys(self, metadata: ProviderMetadata, *, refresh: bool = False) -> KeySet:
        """The issuer's signing keys. ``refresh`` re-fetches them (key rotation), at most
        once per ``_JWKS_REFRESH_INTERVAL`` per issuer."""
        uri = metadata.jwks_uri
        cached = self._keys.get(uri)
        now = time.monotonic()
        if cached is not None:
            if not refresh and now - cached.fetched_at < self.ttl:
                return cached.value
            if refresh and now - self._last_refresh.get(uri, -_JWKS_REFRESH_INTERVAL) < (
                _JWKS_REFRESH_INTERVAL
            ):
                return cached.value
        async with self._lock:
            if refresh:
                self._last_refresh[uri] = now
            document = await self._get_json(uri)
            try:
                key_set = KeySet.import_key_set(document)  # type: ignore[arg-type]
            except (JoseError, ValueError, TypeError, KeyError):
                raise OIDCError(OIDCErrorCode.DISCOVERY_FAILED) from None
            self._keys[uri] = _Cached(key_set, time.monotonic())
            return key_set

    def clear(self) -> None:
        self._metadata.clear()
        self._keys.clear()
        self._last_refresh.clear()
