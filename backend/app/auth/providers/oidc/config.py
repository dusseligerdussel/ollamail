"""Validated provider configuration, from the environment or the database."""

import re
from dataclasses import dataclass, field
from typing import Literal

from app.auth.providers.oidc.models import OIDCProviderRecord
from app.auth.providers.oidc.presets import OIDCPreset, validate_issuer, validate_preset
from app.auth.provisioning import ProvisioningPolicy, normalize_domains
from app.core.config import OIDCProviderSettings

NAME_PATTERN = r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$"
_NAME = re.compile(NAME_PATTERN)
PROVIDER_PREFIX = "oidc:"

ConfigSource = Literal["env", "db"]


@dataclass(frozen=True)
class OIDCConfig:
    name: str
    display_name: str
    preset: OIDCPreset
    issuer: str
    client_id: str
    client_secret: str | None = field(default=None, repr=False)
    scopes: tuple[str, ...] = ("openid", "email", "profile")
    enabled: bool = True
    auto_provision: bool = True
    link_by_email: bool = False
    allowed_domains: frozenset[str] = frozenset()
    groups_claim: str | None = "groups"
    allowed_tenants: frozenset[str] = frozenset()
    hosted_domains: frozenset[str] = frozenset()
    source: ConfigSource = "db"

    @property
    def provider_name(self) -> str:
        return PROVIDER_PREFIX + self.name

    @property
    def policy(self) -> ProvisioningPolicy:
        return ProvisioningPolicy(
            auto_provision=self.auto_provision,
            link_by_email=self.link_by_email,
            allowed_domains=self.allowed_domains,
        )


def validate_config(
    *,
    name: str,
    preset: OIDCPreset,
    issuer: str,
    scopes: list[str] | tuple[str, ...],
    allowed_tenants: list[str],
    hosted_domains: list[str],
    allow_http: bool,
) -> None:
    """Raise ``ValueError`` if the configuration is unusable or unsafe."""
    if not _NAME.match(name):
        raise ValueError("name must be 1-32 lower-case letters, digits or hyphens")
    validate_issuer(issuer, allow_http=allow_http)
    if "openid" not in scopes:
        raise ValueError("scopes must include 'openid'")
    validate_preset(preset, issuer, allowed_tenants, hosted_domains)


def _scopes(scopes: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(s.strip() for s in scopes if s.strip()))


def from_settings(name: str, value: OIDCProviderSettings, *, allow_http: bool) -> OIDCConfig:
    preset = OIDCPreset(value.preset)
    scopes = _scopes(value.scopes)
    validate_config(
        name=name,
        preset=preset,
        issuer=value.issuer,
        scopes=scopes,
        allowed_tenants=value.allowed_tenants,
        hosted_domains=value.hosted_domains,
        allow_http=allow_http,
    )
    return OIDCConfig(
        name=name,
        display_name=value.display_name,
        preset=preset,
        issuer=value.issuer,
        client_id=value.client_id,
        client_secret=value.client_secret.get_secret_value() if value.client_secret else None,
        scopes=scopes,
        enabled=value.enabled,
        auto_provision=value.auto_provision,
        link_by_email=value.link_by_email,
        allowed_domains=normalize_domains(value.allowed_domains),
        groups_claim=value.groups_claim or None,
        allowed_tenants=frozenset(t.lower() for t in value.allowed_tenants),
        hosted_domains=normalize_domains(value.hosted_domains),
        source="env",
    )


def from_record(record: OIDCProviderRecord) -> OIDCConfig:
    return OIDCConfig(
        name=record.name,
        display_name=record.display_name,
        preset=record.preset,
        issuer=record.issuer,
        client_id=record.client_id,
        client_secret=record.client_secret,
        scopes=tuple(record.scopes),
        enabled=record.enabled,
        auto_provision=record.auto_provision,
        link_by_email=record.link_by_email,
        allowed_domains=frozenset(record.allowed_domains),
        groups_claim=record.groups_claim,
        allowed_tenants=frozenset(record.allowed_tenants),
        hosted_domains=frozenset(record.hosted_domains),
        source="db",
    )
