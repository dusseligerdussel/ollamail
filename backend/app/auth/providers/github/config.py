"""Validated provider configuration and the URLs of the GitHub instance."""

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from app.auth.providers.github.models import GitHubProviderRecord
from app.auth.provisioning import ProvisioningPolicy

PROVIDER_PREFIX = "github:"
GITHUB_WEB = "https://github.com"
GITHUB_API = "https://api.github.com"
# User and organization logins: alphanumerics and single hyphens, at most 39 characters.
ORG_PATTERN = r"^[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?$"
# "<org>/<team-slug>"; slugs are lower-case letters, digits, "-", "_" and ".".
TEAM_PATTERN = r"^[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?/[a-z0-9_.-]{1,200}$"


def normalize_base_url(value: str | None) -> str | None:
    """The GitHub Enterprise Server URL without trailing slash; ``None`` for github.com.

    Raises ``ValueError`` for anything but a plain ``https://host[/path]`` URL.
    """
    if value is None or not value.strip():
        return None
    value = value.strip().rstrip("/")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise ValueError("base_url must be an https URL like https://github.example.org") from None
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or (port is not None and not 0 < port < 65536)
    ):
        raise ValueError("base_url must be an https URL like https://github.example.org")
    if parts.hostname in {"github.com", "www.github.com", "api.github.com"}:
        return None
    return value


@dataclass(frozen=True)
class GitHubConfig:
    name: str
    display_name: str
    client_id: str
    client_secret: str = field(repr=False)
    base_url: str | None = None
    enabled: bool = True
    auto_provision: bool = True
    link_by_email: bool = False
    allowed_domains: frozenset[str] = frozenset()
    allowed_organizations: frozenset[str] = frozenset()
    allowed_teams: frozenset[str] = frozenset()

    @property
    def provider_name(self) -> str:
        return PROVIDER_PREFIX + self.name

    @property
    def web_url(self) -> str:
        return self.base_url or GITHUB_WEB

    @property
    def api_url(self) -> str:
        # GitHub Enterprise Server serves the REST API below /api/v3.
        return f"{self.base_url}/api/v3" if self.base_url else GITHUB_API

    @property
    def restricted(self) -> bool:
        return bool(self.allowed_organizations or self.allowed_teams)

    @property
    def policy(self) -> ProvisioningPolicy:
        return ProvisioningPolicy(
            auto_provision=self.auto_provision,
            link_by_email=self.link_by_email,
            allowed_domains=self.allowed_domains,
        )


def from_record(record: GitHubProviderRecord) -> GitHubConfig:
    return GitHubConfig(
        name=record.name,
        display_name=record.display_name,
        client_id=record.client_id,
        client_secret=record.client_secret,
        base_url=record.base_url,
        enabled=record.enabled,
        auto_provision=record.auto_provision,
        link_by_email=record.link_by_email,
        allowed_domains=frozenset(record.allowed_domains),
        allowed_organizations=frozenset(record.allowed_organizations),
        allowed_teams=frozenset(record.allowed_teams),
    )


_ORG = re.compile(ORG_PATTERN)
_TEAM = re.compile(TEAM_PATTERN)


def normalize_organization(value: str) -> str:
    org = value.strip().lower()
    if not _ORG.match(org):
        raise ValueError("invalid organization login")
    return org


def normalize_team(value: str) -> str:
    team = value.strip().lower()
    if not _TEAM.match(team):
        raise ValueError("teams must look like <organization>/<team-slug>")
    return team
