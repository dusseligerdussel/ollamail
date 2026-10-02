"""API schemas for LDAP directories (admin) and the LDAP login."""

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

from app.auth.providers.ldap.settings import DIRECTORY_NAME_PATTERN, LdapDirectorySettings
from app.users.models import UserRole
from app.users.schemas import DisplayName, Password

DirectoryName = Annotated[str, StringConstraints(pattern=DIRECTORY_NAME_PATTERN)]
Login = Annotated[str, Field(min_length=1, max_length=256)]
# Write-only; never returned by the API.
BindPassword = Annotated[str, Field(min_length=1, max_length=1024)]


class LdapDirectoryUpdate(BaseModel):
    """Replaces the configuration; without ``bind_password`` the stored one is kept."""

    display_name: DisplayName
    enabled: bool = True
    settings: LdapDirectorySettings
    bind_password: BindPassword | None = None


class LdapDirectoryCreate(LdapDirectoryUpdate):
    # Part of the provider key ``ldap:<name>`` and of the login URL; cannot be changed.
    name: DirectoryName
    bind_password: BindPassword


class LdapDirectoryRead(BaseModel):
    id: uuid.UUID
    name: str
    # Key in ``GET /api/auth/providers`` and ``auth_identities.provider``.
    provider: str
    display_name: str
    enabled: bool
    settings: LdapDirectorySettings
    bind_password_set: bool
    created_at: datetime
    updated_at: datetime


class LdapServerCheck(BaseModel):
    url: str
    ok: bool
    # unreachable, tls_failed, starttls_failed, connection_lost, service_bind_failed,
    # bind_failed
    error: str | None
    latency_ms: int


class LdapConnectionTest(BaseModel):
    # True if at least one server works (logins are possible).
    ok: bool
    servers: list[LdapServerCheck]


class LdapUserLookupRequest(BaseModel):
    login: Login


class LdapUserLookup(BaseModel):
    """What a login of this user would yield (no password check)."""

    found: bool
    # Error code if the lookup itself failed (see ``LdapServerCheck.error``, plus
    # search_failed, subject_attribute_missing).
    error: str | None = None
    dn: str | None = None
    subject: str | None = None
    email: str | None = None
    display_name: str | None = None
    groups: list[str] = Field(default_factory=list)
    # Account disabled in the directory (AD userAccountControl).
    disabled: bool = False
    # Member of one of ``allowed_groups`` (or no restriction configured).
    allowed: bool = False
    # Role from ``admin_groups``; null if the directory does not manage roles.
    role: UserRole | None = None


class LdapLoginRequest(BaseModel):
    # Whatever ``user_filter`` matches, e.g. sAMAccountName or userPrincipalName.
    username: Login
    password: Password
