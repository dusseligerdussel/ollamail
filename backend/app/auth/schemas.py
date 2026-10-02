"""API schemas for setup, login and sessions."""

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

from app.auth.models import MfaEnforcement
from app.auth.providers import AuthProviderKind
from app.users.models import UserRole
from app.users.schemas import DisplayName, Email, Language, Password, TimeZone


class SetupStatus(BaseModel):
    # False until the first admin exists; the frontend then shows the setup wizard.
    initialized: bool


class SetupRequest(BaseModel):
    setup_token: str = Field(min_length=1, max_length=256)
    email: Email
    display_name: DisplayName
    password: Password
    language: Language = "en"
    timezone: TimeZone = "UTC"


class LoginRequest(BaseModel):
    # Not validated as e-mail address: invalid input must behave like a wrong password.
    email: str = Field(min_length=1, max_length=320)
    password: Password


class RegisterRequest(BaseModel):
    email: Email
    display_name: DisplayName
    password: Password
    language: Language = "en"
    timezone: TimeZone = "UTC"


class AuthProviderInfo(BaseModel):
    name: str
    display_name: str
    kind: AuthProviderKind
    # Redirect providers: path below the API root that starts the login (browser navigation,
    # optional query parameter ``return_to`` with a relative UI path).
    login_path: str | None = None


class AuthProviders(BaseModel):
    local_login: bool
    local_registration: bool
    # Sign-in with a passkey instead of a password (local accounts, WebAuthn configured).
    passkey_login: bool = False
    # External providers (OIDC, GitHub, LDAP); empty until they are configured.
    providers: list[AuthProviderInfo]


class SessionRead(BaseModel):
    id: uuid.UUID
    provider: str
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    user_agent: str | None
    # The session of this request.
    current: bool


# -- Administration (#33) ----------------------------------------------------------------

GroupName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
ProviderKey = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]*(:[a-z0-9_-]+)?$")]


class AdminAccess(BaseModel):
    # Active admins with at least one working sign-in (see app.auth.admin_access).
    usable_admins: int
    # Providers the signed-in admin can currently sign in with (``local``, ``oidc:entra``).
    own_providers: list[str]


class AuthSettingsRead(BaseModel):
    local_login_enabled: bool
    # OLLAMAIL_AUTH_LOCAL_REGISTRATION (environment, read-only).
    local_registration: bool
    # Provider types this instance can configure in the admin UI (``oidc``, ``ldap``,
    # ``github`` once available).
    provider_kinds: list[str]
    admin_access: AdminAccess
    # Local accounts that must use a second factor (#96): off, admins, all.
    mfa_enforcement: MfaEnforcement = MfaEnforcement.OFF


class AuthSettingsUpdate(BaseModel):
    local_login_enabled: bool | None = None
    mfa_enforcement: MfaEnforcement | None = None


class RoleMappingRuleFields(BaseModel):
    group: GroupName
    # Provider key (``oidc:entra``, ``ldap:corp``); null: all providers.
    provider: ProviderKey | None = None
    role: UserRole


class RoleMappingRuleRead(RoleMappingRuleFields):
    id: uuid.UUID


class RoleMappingUpdate(BaseModel):
    """Replaces the mapping (all rules)."""

    enabled: bool
    default_role: UserRole = UserRole.USER
    rules: list[RoleMappingRuleFields] = Field(default=[], max_length=200)


class RoleMappingRead(BaseModel):
    enabled: bool
    default_role: UserRole
    rules: list[RoleMappingRuleRead]


class RoleMappingTest(BaseModel):
    provider: ProviderKey
    groups: list[GroupName] = Field(default=[], max_length=500)


class RoleMappingTestResult(BaseModel):
    # Role a login with these groups would get; null: unchanged (mapping off).
    role: UserRole | None
    # Groups of the matching rules (as configured).
    matched_groups: list[str]
