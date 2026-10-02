"""Base URL handling for github.com and GitHub Enterprise Server."""

import pytest

from app.auth.providers.github.config import GitHubConfig, normalize_base_url


def _config(base_url: str | None) -> GitHubConfig:
    return GitHubConfig(
        name="gh", display_name="GitHub", client_id="id", client_secret="s", base_url=base_url
    )


def test_github_com_urls() -> None:
    config = _config(None)
    assert config.web_url == "https://github.com"
    assert config.api_url == "https://api.github.com"
    assert config.provider_name == "github:gh"


def test_enterprise_server_urls() -> None:
    config = _config("https://github.example.org")
    assert config.web_url == "https://github.example.org"
    assert config.api_url == "https://github.example.org/api/v3"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("  ", None),
        ("https://github.com", None),
        ("https://api.github.com/", None),
        ("https://github.example.org/", "https://github.example.org"),
        ("https://git.example.org:8443/ghe", "https://git.example.org:8443/ghe"),
    ],
)
def test_normalize_base_url(value: str | None, expected: str | None) -> None:
    assert normalize_base_url(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "http://github.example.org",
        "github.example.org",
        "https://",
        "https://u:p@github.example.org",
        "https://github.example.org/#x",
        "https://github.example.org:99999",
    ],
)
def test_invalid_base_url(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_base_url(value)
