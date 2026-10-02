"""API schemas for GitHub provider administration."""

from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field, StringConstraints

from app.auth.providers.github.config import normalize_organization, normalize_team
from app.auth.providers.oidc.schemas import (
    ClientId,
    ClientSecret,
    DisplayName,
    Domain,
    ProviderName,
)

BaseUrl = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2048)]
Organization = Annotated[str, Field(max_length=64), AfterValidator(normalize_organization)]
Team = Annotated[str, Field(max_length=255), AfterValidator(normalize_team)]


class GitHubProviderFields(BaseModel):
    display_name: DisplayName = "GitHub"
    # GitHub Enterprise Server (e.g. https://github.example.org); null for github.com.
    base_url: BaseUrl | None = None
    client_id: ClientId
    enabled: bool = True
    # Create unknown users on their first login (role "user").
    auto_provision: bool = True
    # Link to an existing user with the same (verified, primary) e-mail address.
    link_by_email: bool = False
    # E-mail domains allowed to sign in; empty allows all.
    allowed_domains: list[Domain] = Field(default=[], max_length=100)
    # Only members of these organizations (logins) may sign in; empty: no restriction.
    allowed_organizations: list[Organization] = Field(default=[], max_length=100)
    # Only members of these teams ("<org>/<team-slug>") may sign in; empty: no restriction.
    # With both lists set, membership in either is enough.
    allowed_teams: list[Team] = Field(default=[], max_length=100)


class GitHubProviderCreate(GitHubProviderFields):
    name: ProviderName
    # Write-only; GitHub requires the client secret for the code exchange.
    client_secret: ClientSecret


class GitHubProviderUpdate(BaseModel):
    """Fields to change; omitted fields stay. ``base_url: null`` switches to github.com."""

    display_name: DisplayName | None = None
    base_url: BaseUrl | None = None
    client_id: ClientId | None = None
    client_secret: ClientSecret | None = None
    enabled: bool | None = None
    auto_provision: bool | None = None
    link_by_email: bool | None = None
    allowed_domains: list[Domain] | None = Field(default=None, max_length=100)
    allowed_organizations: list[Organization] | None = Field(default=None, max_length=100)
    allowed_teams: list[Team] | None = Field(default=None, max_length=100)


class GitHubProviderRead(GitHubProviderFields):
    name: str
    # Key in identities and sessions: "github:<name>".
    provider: str
    has_client_secret: bool
    # Register this URL as the callback URL of the OAuth App / GitHub App.
    redirect_uri: str
    created_at: datetime
    updated_at: datetime
