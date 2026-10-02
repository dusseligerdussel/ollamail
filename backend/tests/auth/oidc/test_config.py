"""Provider configuration from the environment (GitOps)."""

import pytest

from app.auth.providers.oidc.store import OIDCProviderStore
from app.core.config import AuthSettings, OIDCProviderSettings, Settings


def _settings(**provider: object) -> Settings:
    values: dict[str, object] = {
        "display_name": "Corp",
        "issuer": "https://idp.example.org/realms/corp",
        "client_id": "ollamail",
    }
    values.update(provider)
    return Settings(
        auth=AuthSettings(oidc_providers={"corp": OIDCProviderSettings.model_validate(values)})
    )


def test_env_providers_are_parsed_from_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "OLLAMAIL_AUTH_OIDC_PROVIDERS",
        '{"corp": {"display_name": "Corp", "preset": "google", '
        '"issuer": "https://accounts.google.com", "client_id": "id", '
        '"client_secret": "env-secret-value", '
        '"hosted_domains": ["Example.org"], "groups_claim": null}}',
    )

    store = OIDCProviderStore(Settings(auth=AuthSettings()))

    config = store.env_configs["corp"]
    assert config.source == "env"
    assert config.provider_name == "oidc:corp"
    assert config.client_secret == "env-secret-value"
    assert config.hosted_domains == frozenset({"example.org"})
    assert config.groups_claim is None
    assert "env-secret-value" not in repr(config)


@pytest.mark.parametrize(
    "overrides",
    [
        {"issuer": "http://idp.example.org"},
        {"preset": "entra", "issuer": "https://login.microsoftonline.com/common/v2.0"},
        {"scopes": ["profile"]},
    ],
)
def test_invalid_env_provider_fails_at_start_up(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=r"OLLAMAIL_AUTH_OIDC_PROVIDERS\[corp\]"):
        OIDCProviderStore(_settings(**overrides))


def test_http_issuer_only_with_explicit_opt_in() -> None:
    settings = _settings(issuer="http://localhost:8080/realms/dev")
    settings.auth.oidc_allow_insecure_http = True

    assert OIDCProviderStore(settings).env_configs["corp"].issuer.startswith("http://")


def test_invalid_env_name_fails() -> None:
    settings = _settings()
    settings.auth.oidc_providers = {"Bad Name": settings.auth.oidc_providers["corp"]}

    with pytest.raises(ValueError, match="name"):
        OIDCProviderStore(settings)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", None),
        ("https://mail.example.org/", "https://mail.example.org"),
        ("http://localhost:8080", "http://localhost:8080"),
    ],
)
def test_public_url(value: str, expected: str | None) -> None:
    assert AuthSettings(public_url=value).public_url == expected


@pytest.mark.parametrize("value", ["mail.example.org", "ftp://x", "https://a@b", "https://x?y"])
def test_invalid_public_url(value: str) -> None:
    with pytest.raises(ValueError, match="public_url"):
        AuthSettings(public_url=value)
