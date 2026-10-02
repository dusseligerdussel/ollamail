"""Helpers of the redirect login flow (no database)."""

import time

import pytest

from app.auth.redirect_flow import (
    LoginFlow,
    idp_error_code,
    safe_return_to,
    seal,
    unseal,
)
from app.core.config import SecuritySettings, Settings
from app.core.crypto import generate_key


def _settings() -> Settings:
    return Settings(security=SecuritySettings.model_validate({"secret_key": generate_key()}))


def _flow(**overrides: object) -> LoginFlow:
    values: dict[str, object] = {
        "provider": "oidc:corp",
        "state": "state",
        "nonce": "nonce",
        "code_verifier": "verifier",
        "redirect_uri": "https://mail.example.org/api/auth/oidc/corp/callback",
        "return_to": "/",
        "expires_at": int(time.time()) + 60,
    }
    values.update(overrides)
    return LoginFlow(**values)  # type: ignore[arg-type]


def test_seal_roundtrip_and_confidentiality() -> None:
    settings = _settings()
    flow = _flow()

    sealed = seal(settings, flow)

    assert unseal(settings, sealed) == flow
    assert "verifier" not in sealed and "state" not in sealed


def test_unseal_rejects_tampering_other_keys_and_expiry() -> None:
    settings = _settings()
    sealed = seal(settings, _flow())

    assert unseal(settings, sealed[:-2] + ("AA" if sealed[-2:] != "AA" else "BB")) is None
    assert unseal(_settings(), sealed) is None
    assert unseal(settings, "not base64 !") is None
    assert unseal(settings, None) is None
    assert unseal(settings, seal(settings, _flow(expires_at=int(time.time()) - 1))) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "/"),
        ("", "/"),
        ("/inbox", "/inbox"),
        ("/inbox?x=1#y", "/inbox?x=1#y"),
        ("https://evil.example/", "/"),
        ("//evil.example", "/"),
        ("/\\evil.example", "/"),
        ("/a\\b", "/"),
        ("/a\nb", "/"),
        ("javascript:alert(1)", "/"),
        ("/" + "a" * 3000, "/"),
    ],
)
def test_safe_return_to(value: str | None, expected: str) -> None:
    assert safe_return_to(value) == expected


def test_idp_error_code_is_sanitised() -> None:
    assert idp_error_code("access_denied") == "access_denied"
    assert idp_error_code("<script>") == "other"
    assert idp_error_code(None) == "other"
