"""API schemas of the classic search: request with filters and the hit list."""

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.mail.api.message_schemas import AddressRead


class SearchFilterParams(BaseModel):
    """Filters set in the UI; each one narrows the mailboxes the user may read."""

    model_config = ConfigDict(extra="forbid")

    mailbox_ids: list[uuid.UUID] | None = Field(default=None, max_length=100)
    category_ids: list[uuid.UUID] | None = Field(default=None, max_length=100)
    # Part of the sender's name or address.
    sender: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None
    # Date of the mail: ``since <= date < until``.
    since: datetime | None = None
    until: datetime | None = None


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    filters: SearchFilterParams = Field(default_factory=SearchFilterParams)
    limit: int = Field(default=30, ge=1, le=100)


class SearchHitRead(BaseModel):
    """The best matching passage of one message."""

    message_id: uuid.UUID
    mailbox_id: uuid.UUID
    thread_id: uuid.UUID | None
    subject: str
    sender: AddressRead | None
    # Received, else sent, else stored.
    date: datetime
    # ``body`` or ``attachment``.
    source: str
    attachment_id: uuid.UUID | None
    attachment_filename: str | None
    # Part of the matching passage, around the first query term found in it.
    excerpt: str
    score: float


class SearchResults(BaseModel):
    hits: list[SearchHitRead]
