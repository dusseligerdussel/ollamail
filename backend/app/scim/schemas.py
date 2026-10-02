"""Admin API schemas for SCIM provisioning (#95)."""

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

ProviderKey = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9:_.-]{0,63}$")]


class ScimTokenRead(BaseModel):
    id: uuid.UUID
    name: str
    # First characters of the token (``olm_scim_ab12``), to tell tokens apart.
    hint: str
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None


class ScimStats(BaseModel):
    users: int
    active_users: int
    groups: int


class ScimSettingsRead(BaseModel):
    enabled: bool
    # Absolute base URL to enter at the IdP (``<public url>/api/scim/v2``).
    endpoint_url: str
    # Sign-in providers that may link a login to a user created by SCIM by verified e-mail.
    link_providers: list[str]
    tokens: list[ScimTokenRead]
    stats: ScimStats


class ScimSettingsUpdate(BaseModel):
    enabled: bool | None = None
    link_providers: list[ProviderKey] | None = Field(default=None, max_length=20)


class ScimTokenCreate(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    # Days until the token stops working; ``None``: no expiry.
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class ScimTokenIssued(BaseModel):
    token: ScimTokenRead
    # The bearer token; shown only in this response.
    secret: str
