"""API schemas for setup, login and sessions."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.auth.providers import AuthProviderKind
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


class AuthProviders(BaseModel):
    local_login: bool
    local_registration: bool
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
