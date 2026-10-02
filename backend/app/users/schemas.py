"""API schemas for users."""

import uuid
from datetime import datetime
from typing import Annotated, Literal
from zoneinfo import available_timezones

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

from app.users.models import UserRole

Language = Literal["de", "en"]
# Upper bound only; the minimum length is a setting (``OLLAMAIL_AUTH_PASSWORD_MIN_LENGTH``).
Password = Annotated[str, Field(min_length=1, max_length=1024)]
DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]


def normalize_email(value: str) -> str:
    """Trim and lower-case; rejects obviously invalid addresses. Deliverability is not
    checked (local first: no DNS lookups)."""
    email = value.strip().lower()
    local, at, domain = email.rpartition("@")
    if not at or not local or "." not in domain or domain.startswith(".") or " " in email:
        raise ValueError("invalid e-mail address")
    return email


def _check_timezone(value: str) -> str:
    if value not in available_timezones():
        raise ValueError("unknown time zone")
    return value


Email = Annotated[str, Field(max_length=320), AfterValidator(normalize_email)]
TimeZone = Annotated[str, Field(max_length=64), AfterValidator(_check_timezone)]


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    display_name: str
    role: UserRole
    language: str
    timezone: str
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None


class UserCreate(BaseModel):
    """A new local account (admin)."""

    email: Email
    display_name: DisplayName
    password: Password
    role: UserRole = UserRole.USER
    language: Language = "en"
    timezone: TimeZone = "UTC"


class ProfileUpdate(BaseModel):
    """Fields a user may change on their own account."""

    display_name: DisplayName | None = None
    language: Language | None = None
    timezone: TimeZone | None = None


class AdminUserRead(UserRead):
    """A user in the admin list: account data and sign-in state, never mailbox data."""

    # Providers the user has identities at (``local``, ``oidc:entra``, ``ldap:corp``).
    providers: list[str]
    # Local account without password whose invitation is still valid.
    invitation_pending: bool
    active_sessions: int


class AdminUserUpdate(BaseModel):
    """Changes by an admin; omitted fields stay. Refused (409) if no admin could sign in
    afterwards."""

    role: UserRole | None = None
    is_active: bool | None = None


class UserInvite(BaseModel):
    """A local account that sets its own password via an invitation link."""

    email: Email
    display_name: DisplayName
    role: UserRole = UserRole.USER
    language: Language = "en"
    timezone: TimeZone = "UTC"


class InvitationIssued(BaseModel):
    user: AdminUserRead
    # One-time link to pass on to the user; shown only once.
    invite_url: str
    expires_at: datetime
