"""OAuth helpers and ``clientState`` of the Microsoft Graph provider (no network)."""

import base64
import hashlib
import uuid
from urllib.parse import parse_qs, urlsplit

from app.auth.csrf import CSRFMiddleware
from app.core.config import SecuritySettings, Settings
from app.core.crypto import generate_key
from app.mail.providers.graph_auth import (
    TokenSet,
    app_scopes,
    authorization_url,
    code_challenge,
    code_verifier,
    delegated_scopes,
)
from app.mail.providers.graph_webhook import client_state, verify_client_state
from tests.mail.graph_helpers import SECURITY, graph_settings


def test_pkce_challenge_is_s256_of_the_verifier() -> None:
    verifier = code_verifier()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    assert code_challenge(verifier) == expected.rstrip(b"=").decode()
    assert code_verifier() != verifier


def test_authorization_url() -> None:
    settings = graph_settings(tenant_id="organizations")
    url = authorization_url(
        settings,
        redirect_uri="https://mail.example.com/api/mail/graph/callback",
        state="state-1",
        verifier="v" * 64,
        scopes=delegated_scopes(settings),
        login_hint="erika@example.com",
    )
    parts = urlsplit(url)
    assert parts.netloc == "login.microsoftonline.com"
    assert parts.path == "/organizations/oauth2/v2.0/authorize"
    query = parse_qs(parts.query)
    assert query["client_id"] == [settings.client_id]
    assert query["state"] == ["state-1"]
    assert query["code_challenge"] == [code_challenge("v" * 64)]
    assert query["login_hint"] == ["erika@example.com"]
    # The client secret never goes to the browser.
    assert "test-client-secret" not in url


def test_scopes_follow_the_configured_cloud() -> None:
    settings = graph_settings(api_url="https://graph.microsoft.us/v1.0")
    assert delegated_scopes(settings, shared=True) == [
        "offline_access",
        "https://graph.microsoft.us/User.Read",
        "https://graph.microsoft.us/Mail.ReadWrite",
        "https://graph.microsoft.us/Mail.ReadWrite.Shared",
    ]
    assert app_scopes(settings) == ["https://graph.microsoft.us/.default"]


def test_token_set_round_trip_and_expiry() -> None:
    tokens = TokenSet(access_token="a", expires_at=1000, refresh_token="r")
    assert TokenSet.from_credentials(tokens.to_credentials()) == tokens
    assert tokens.valid(600) and not tokens.valid(800)
    assert repr(tokens) == "TokenSet(expires_at=1000)"
    assert not TokenSet.from_credentials({"expires_at": "x"}).valid(0)


def test_client_state_is_bound_to_the_mailbox_and_key() -> None:
    mailbox_id = uuid.uuid4()
    value = client_state(SECURITY, mailbox_id)
    assert len(value) <= 128
    assert verify_client_state(SECURITY, value) == mailbox_id
    other_key = SecuritySettings.model_validate({"secret_key": generate_key()})
    assert verify_client_state(other_key, value) is None
    mac = value.split(".")[1]
    assert verify_client_state(SECURITY, f"{uuid.uuid4()}.{mac}") is None
    for garbage in [None, 1, "", "x", "a.b.c", f"not-a-uuid.{mac}"]:
        assert verify_client_state(SECURITY, garbage) is None


def test_csrf_exemption_ignores_the_root_path() -> None:
    middleware = CSRFMiddleware(
        app=None,  # type: ignore[arg-type]
        settings=Settings(),
        exempt_paths=["/mail/graph/notifications"],
    )
    assert middleware._exempt({"path": "/mail/graph/notifications"})
    assert middleware._exempt({"path": "/api/mail/graph/notifications", "root_path": "/api"})
    assert not middleware._exempt({"path": "/mail/graph/connect", "root_path": ""})
    assert not middleware._exempt({"path": "/mail/graph/notifications/x"})
