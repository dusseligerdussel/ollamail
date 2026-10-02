"""API schemas of the triage endpoints."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.triage.models import TriageSource, WriteBackMode

CategoryScope = Literal["organization", "user"]


class CategoryRead(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    # Set for built-in categories; the UI shows a translated name for these.
    builtin_key: str | None
    scope: CategoryScope
    hidden: bool
    position: int


class CategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)

    @field_validator("name")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name must not be blank")
        return value


class CategoryUpdate(BaseModel):
    """Own categories: all fields. Organisation categories: ``hidden`` only."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    hidden: bool | None = None


class CategoryOrder(BaseModel):
    """All visible and hidden categories of the user, in the new order."""

    category_ids: list[uuid.UUID] = Field(min_length=1, max_length=200)


class OrganizationCategoryCreate(CategoryCreate):
    position: int = 0


class OrganizationCategoryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    position: int | None = None


class OrganizationCategoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str
    builtin_key: str | None
    position: int


class TriageRead(BaseModel):
    message_id: uuid.UUID
    # ``None`` if the category was deleted.
    category_id: uuid.UUID | None
    priority: int
    # One sentence by the model; ``None`` for rule decisions and corrections.
    reason: str | None
    # Rule that decided without the model (``list_unsubscribe``, ``sender``, ...).
    rule: str | None
    source: TriageSource
    model: str | None
    prompt_version: str | None
    updated_at: datetime


class TriageCorrection(BaseModel):
    category_id: uuid.UUID
    priority: int = Field(ge=1, le=3, description="1 = high, 2 = normal, 3 = low")


class InboxMessage(BaseModel):
    message_id: uuid.UUID
    mailbox_id: uuid.UUID
    subject: str
    sender_name: str | None
    sender_address: str | None
    received_at: datetime | None
    # ``None`` while the message is not triaged yet.
    priority: int | None
    source: TriageSource | None
    reason: str | None


class InboxGroup(BaseModel):
    # ``None``: not triaged yet, or in a hidden or deleted category.
    category: CategoryRead | None
    total: int
    messages: list[InboxMessage]


class SenderRuleCreate(BaseModel):
    sender: str = Field(
        min_length=3,
        max_length=320,
        description="Address (news@example.org) or domain with @ (@example.org)",
    )
    category_id: uuid.UUID
    priority: int = Field(default=3, ge=1, le=3)

    @field_validator("sender")
    @classmethod
    def _sender(cls, value: str) -> str:
        value = value.strip().lower()
        local, at, domain = value.rpartition("@")
        if not at or not domain or "." not in domain or " " in value or "@" in local:
            raise ValueError("expected an address or @domain")
        return value


class SenderRuleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    sender: str
    category_id: uuid.UUID
    priority: int


class SenderRuleSuggestion(BaseModel):
    sender: str
    category_id: uuid.UUID
    priority: int
    # Corrections of this sender into the category.
    corrections: int


class MailboxTriageSettings(BaseModel):
    write_back: WriteBackMode
