"""Google OAuth token handling of the Gmail provider (synthetic token endpoint, respx)."""

import base64
import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.core.config import GmailSettings
from app.mail.providers.base import AuthenticationError, ConfigurationError, ConnectionFailedError
from app.mail.providers.gmail_auth import (
    SCOPE_MODIFY,
    SCOPE_READONLY,
    TOKEN_URL,
    RefreshTokenSource,
    ServiceAccountTokenSource,
    authorization_url,
    clear_token_cache,
    exchange_code,
    load_service_account_key,
    pkce_pair,
    scope_granted,
)

SETTINGS = GmailSettings(
    client_id="client-id.apps.googleusercontent.com",
    client_secret="test-client-secret",  # type: ignore[arg-type]
    redirect_uri="http://localhost:8080/api/mail/gmail/oauth/callback",
)


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    clear_token_cache()
    yield
    clear_token_cache()


def form(request: httpx.Request) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(request.content.decode()).items()}


class Clock:
    def __init__(self) -> None:
        self.now = 1_790_000_000.0

    def __call__(self) -> float:
        return self.now


async def test_refresh_token_source_caches_until_shortly_before_expiry() -> None:
    clock = Clock()
    with respx.mock() as router:
        route = router.post(TOKEN_URL).mock(
            side_effect=[
                httpx.Response(200, json={"access_token": "at-1", "expires_in": 3599}),
                httpx.Response(200, json={"access_token": "at-2", "expires_in": 3599}),
                httpx.Response(200, json={"access_token": "at-3", "expires_in": 3599}),
            ]
        )
        async with httpx.AsyncClient() as http:
            source = RefreshTokenSource(http, SETTINGS, "test-refresh-token", clock=clock)
            assert await source.token() == "at-1"
            assert await source.token() == "at-1"
            # Another provider instance for the same mailbox shares the cache.
            other = RefreshTokenSource(http, SETTINGS, "test-refresh-token", clock=clock)
            assert await other.token() == "at-1"
            clock.now += 3599 - 100
            assert await source.token() == "at-2"
            assert await source.token(refresh=True) == "at-3"

    assert route.call_count == 3
    assert form(route.calls[0].request) == {
        "grant_type": "refresh_token",
        "refresh_token": "test-refresh-token",
        "client_id": "client-id.apps.googleusercontent.com",
        "client_secret": "test-client-secret",
    }


@pytest.mark.parametrize(
    ("response", "error", "code"),
    [
        (
            httpx.Response(400, json={"error": "invalid_grant", "error_description": "x"}),
            AuthenticationError,
            "token_revoked",
        ),
        (httpx.Response(401, json={"error": "invalid_client"}), AuthenticationError, None),
        (httpx.Response(503), ConnectionFailedError, "token_endpoint_unavailable"),
        (httpx.Response(200, json={"token_type": "Bearer"}), AuthenticationError, None),
    ],
)
async def test_refresh_errors(
    response: httpx.Response, error: type[Exception], code: str | None
) -> None:
    with respx.mock() as router:
        router.post(TOKEN_URL).mock(return_value=response)
        async with httpx.AsyncClient() as http:
            source = RefreshTokenSource(http, SETTINGS, "test-refresh-token")
            with pytest.raises(error) as info:
                await source.token()
    if code is not None:
        assert getattr(info.value, "code", None) == code
    assert "test-refresh-token" not in repr(info.value)


async def test_refresh_network_error() -> None:
    with respx.mock() as router:
        router.post(TOKEN_URL).mock(side_effect=httpx.ConnectTimeout("synthetic"))
        async with httpx.AsyncClient() as http:
            with pytest.raises(ConnectionFailedError):
                await RefreshTokenSource(http, SETTINGS, "rt").token()


def test_refresh_needs_client_configuration() -> None:
    with pytest.raises(ConfigurationError):
        RefreshTokenSource(httpx.AsyncClient(), GmailSettings(), "rt")


# -- service account -----------------------------------------------------------------------


@pytest.fixture
def private_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def key_file(tmp_path: Path, private_key: rsa.RSAPrivateKey) -> Path:
    pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    path = tmp_path / "service-account.json"
    path.write_text(
        json.dumps(
            {
                "type": "service_account",
                "project_id": "test-project",
                "private_key_id": "test-key-id",
                "private_key": pem,
                "client_email": "ollamail@test-project.iam.gserviceaccount.com",
                "token_uri": TOKEN_URL,
            }
        )
    )
    return path


def decode_segment(segment: str) -> Any:
    return json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))


async def test_service_account_signs_delegated_jwt(
    key_file: Path, private_key: rsa.RSAPrivateKey
) -> None:
    clock = Clock()
    with respx.mock() as router:
        route = router.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "sa-at", "expires_in": 3600})
        )
        async with httpx.AsyncClient() as http:
            source = ServiceAccountTokenSource(
                http,
                load_service_account_key(key_file),
                SCOPE_MODIFY,
                subject="test.user@example.com",
                clock=clock,
            )
            assert await source.token() == "sa-at"

    body = form(route.calls[0].request)
    assert body["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
    header, claims, signature = body["assertion"].split(".")
    assert decode_segment(header) == {"alg": "RS256", "typ": "JWT"}
    assert decode_segment(claims) == {
        "iss": "ollamail@test-project.iam.gserviceaccount.com",
        "scope": SCOPE_MODIFY,
        "aud": TOKEN_URL,
        "iat": int(clock.now),
        "exp": int(clock.now) + 3600,
        "sub": "test.user@example.com",
    }
    private_key.public_key().verify(
        base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)),
        f"{header}.{claims}".encode(),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )


async def test_service_account_without_delegation(key_file: Path) -> None:
    with respx.mock() as router:
        router.post(TOKEN_URL).mock(
            return_value=httpx.Response(400, json={"error": "unauthorized_client"})
        )
        async with httpx.AsyncClient() as http:
            source = ServiceAccountTokenSource(
                http, load_service_account_key(key_file), SCOPE_MODIFY, "x@example.com"
            )
            with pytest.raises(AuthenticationError) as info:
                await source.token()
    assert info.value.code == "delegation_denied"


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        json.dumps({"client_email": "x@example.com"}),
        json.dumps({"client_email": "x", "private_key": "k", "token_uri": "http://insecure"}),
    ],
)
def test_invalid_service_account_file(tmp_path: Path, content: str) -> None:
    path = tmp_path / "key.json"
    path.write_text(content)
    with pytest.raises(ConfigurationError) as info:
        load_service_account_key(path)
    assert info.value.code == "service_account_invalid"


# -- connect flow building blocks ------------------------------------------------------------


def test_pkce_pair() -> None:
    verifier, challenge = pkce_pair()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    assert challenge == expected.rstrip(b"=").decode()
    assert pkce_pair()[0] != verifier


def test_authorization_url() -> None:
    url = authorization_url(
        SETTINGS, state="st", code_challenge="ch", login_hint="test.user@example.com"
    )
    parsed = urlparse(url)
    params = {key: values[0] for key, values in parse_qs(parsed.query).items()}
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == (
        "https://accounts.google.com/o/oauth2/v2/auth"
    )
    assert params == {
        "client_id": "client-id.apps.googleusercontent.com",
        "redirect_uri": "http://localhost:8080/api/mail/gmail/oauth/callback",
        "response_type": "code",
        "scope": SCOPE_MODIFY,
        "access_type": "offline",
        "prompt": "consent",
        "state": "st",
        "code_challenge": "ch",
        "code_challenge_method": "S256",
        "login_hint": "test.user@example.com",
    }
    readonly = SETTINGS.model_copy(update={"readonly": True})
    assert f"scope={SCOPE_READONLY}".replace(":", "%3A").replace("/", "%2F") in (
        authorization_url(readonly, state="s", code_challenge="c")
    )
    with pytest.raises(ConfigurationError):
        authorization_url(GmailSettings(), state="s", code_challenge="c")


async def test_exchange_code() -> None:
    with respx.mock() as router:
        route = router.post(TOKEN_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "at",
                    "expires_in": 3599,
                    "refresh_token": "rt",
                    "scope": SCOPE_MODIFY,
                    "token_type": "Bearer",
                },
            )
        )
        async with httpx.AsyncClient() as http:
            grant = await exchange_code(http, SETTINGS, "auth-code", "verifier")

    assert (grant.access_token, grant.refresh_token, grant.scopes) == (
        "at",
        "rt",
        frozenset({SCOPE_MODIFY}),
    )
    assert form(route.calls[0].request) == {
        "grant_type": "authorization_code",
        "code": "auth-code",
        "code_verifier": "verifier",
        "client_id": "client-id.apps.googleusercontent.com",
        "client_secret": "test-client-secret",
        "redirect_uri": "http://localhost:8080/api/mail/gmail/oauth/callback",
    }
    assert "rt" not in repr(grant)


def test_scope_granted() -> None:
    assert scope_granted(SETTINGS, frozenset({SCOPE_MODIFY}))
    assert not scope_granted(SETTINGS, frozenset({SCOPE_READONLY}))
    assert not scope_granted(SETTINGS, frozenset())
    readonly = SETTINGS.model_copy(update={"readonly": True})
    assert scope_granted(readonly, frozenset({SCOPE_READONLY}))
    assert scope_granted(readonly, frozenset({SCOPE_MODIFY}))
