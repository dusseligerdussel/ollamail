"""API schemas for OIDC provider administration and logout."""

import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field, StringConstraints

from app.auth.providers.oidc.config import NAME_PATTERN
from app.auth.providers.oidc.presets import OIDCPreset

ProviderName = Annotated[str, StringConstraints(pattern=NAME_PATTERN, max_length=32)]
DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
Issuer = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2048)]
ClientId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
# Write-only; never returned.
ClientSecret = Annotated[str, Field(min_length=1, max_length=4096)]
Scope = Annotated[str, StringConstraints(pattern=r"^[\x21\x23-\x5b\x5d-\x7e]{1,64}$")]
_DOMAIN = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")


def _domain(value: str) -> str:
    domain = value.strip().lower().lstrip("@")
    if not _DOMAIN.match(domain):
        raise ValueError("invalid domain")
    return domain


Domain = Annotated[str, Field(max_length=254), AfterValidator(_domain)]
TenantId = Annotated[
    str, StringConstraints(strip_whitespace=True, to_lower=True, min_length=1, max_length=64)
]
ClaimName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]


class OIDCProviderFields(BaseModel):
    display_name: DisplayName
    preset: OIDCPreset = OIDCPreset.GENERIC
    issuer: Issuer
    client_id: ClientId
    scopes: list[Scope] = Field(default=["openid", "email", "profile"], max_length=32)
    enabled: bool = True
    # Create unknown users on their first login (role "user").
    auto_provision: bool = True
    # Link to an existing user with the same e-mail address; only for verified addresses.
    link_by_email: bool = False
    # E-mail domains allowed to sign in; empty allows all.
    allowed_domains: list[Domain] = Field(default=[], max_length=100)
    # Claim with the user's groups (stored for the role mapping); null for none.
    groups_claim: ClaimName | None = "groups"
    # Entra ID: allowed tenant IDs (required for organizations/common issuers).
    allowed_tenants: list[TenantId] = Field(default=[], max_length=100)
    # Google Workspace: allowed hosted domains (hd claim).
    hosted_domains: list[Domain] = Field(default=[], max_length=100)


class OIDCProviderCreate(OIDCProviderFields):
    name: ProviderName
    # Omit for public clients (PKCE only).
    client_secret: ClientSecret | None = None


class OIDCProviderUpdate(BaseModel):
    """Fields to change; omitted fields stay. ``client_secret: null`` removes the secret."""

    display_name: DisplayName | None = None
    preset: OIDCPreset | None = None
    issuer: Issuer | None = None
    client_id: ClientId | None = None
    client_secret: ClientSecret | None = None
    scopes: list[Scope] | None = Field(default=None, max_length=32)
    enabled: bool | None = None
    auto_provision: bool | None = None
    link_by_email: bool | None = None
    allowed_domains: list[Domain] | None = Field(default=None, max_length=100)
    groups_claim: ClaimName | None = None
    allowed_tenants: list[TenantId] | None = Field(default=None, max_length=100)
    hosted_domains: list[Domain] | None = Field(default=None, max_length=100)


class OIDCProviderRead(OIDCProviderFields):
    name: str
    # Key in identities and sessions: "oidc:<name>".
    provider: str
    # "env": from OLLAMAIL_AUTH_OIDC_PROVIDERS, read-only. "db": managed in the admin API.
    source: Literal["env", "db"]
    has_client_secret: bool
    # Register this URL at the IdP.
    redirect_uri: str
    created_at: datetime | None
    updated_at: datetime | None


class OIDCPresetRead(BaseModel):
    preset: OIDCPreset
    label: str
    issuer_template: str
    scopes: list[str]
    groups_claim: str | None
    # Preset-specific settings (allowed_tenants, hosted_domains).
    fields: list[str]
    docs: str


class LogoutResult(BaseModel):
    # Where the browser should go to end the IdP session as well; null if not supported.
    redirect_url: str | None
