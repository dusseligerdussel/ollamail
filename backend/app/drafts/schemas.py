"""API schemas of reply drafts: requests, the draft, settings and the generation stream."""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.drafts.models import DraftStatus
from app.mail.compose import InvalidAddressError, validate_address
from app.mail.providers.base import OutgoingAddress

Instruction = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
Body = Annotated[str, StringConstraints(max_length=100_000)]


class RecipientRead(BaseModel):
    name: str | None = None
    address: str


class Recipient(BaseModel):
    """A recipient entered by the user; must be usable in a header."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None
    address: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=320)]

    @model_validator(mode="after")
    def _usable_in_header(self) -> "Recipient":
        try:
            validate_address(OutgoingAddress(address=self.address, name=self.name))
        except InvalidAddressError:
            raise ValueError("invalid address") from None
        return self


class DraftCreate(BaseModel):
    """A draft written by hand (no model call)."""

    model_config = ConfigDict(extra="forbid")

    message_id: uuid.UUID
    reply_all: bool = False
    body: Body = ""


class DraftGenerate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The mail to answer.
    message_id: uuid.UUID
    # What the reply should say, e.g. "kurz zusagen" or "höflich absagen".
    instruction: Instruction | None = None
    reply_all: bool = False
    # Replace the text of this draft (of the same mail) instead of starting a new one.
    draft_id: uuid.UUID | None = None


class DraftUpdate(BaseModel):
    """Changes by the user; omitted fields stay. Changing ``reply_all`` recomputes the
    recipients unless ``to``/``cc`` are given as well."""

    model_config = ConfigDict(extra="forbid")

    body: Body | None = None
    subject: Annotated[str, StringConstraints(max_length=998, pattern=r"^[^\r\n]*$")] | None = None
    to: list[Recipient] | None = Field(default=None, max_length=100)
    cc: list[Recipient] | None = Field(default=None, max_length=100)
    reply_all: bool | None = None
    quote_original: bool | None = None


class DraftRead(BaseModel):
    id: uuid.UUID
    # The answered mail; ``null`` if it was deleted (the draft cannot be sent then).
    message_id: uuid.UUID | None
    mailbox_id: uuid.UUID
    thread_id: uuid.UUID | None
    status: DraftStatus
    reply_all: bool
    to: list[RecipientRead]
    cc: list[RecipientRead]
    subject: str
    body: str
    quote_original: bool
    instruction: str | None
    language: str | None
    # Model of the last generation; ``null`` for drafts written by hand.
    model: str | None
    created_at: datetime
    updated_at: datetime
    sent_at: datetime | None
    # The user may send from this mailbox (owner; shared mailboxes are read-only).
    can_send: bool


class DraftSettingsRead(BaseModel):
    signature: str
    # Own sent mails are passed to the model as style examples.
    style_examples: bool
    # Style examples are enabled on this instance (``OLLAMAIL_DRAFTS_STYLE_EXAMPLES`` > 0).
    style_examples_available: bool


class DraftSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signature: Annotated[str, StringConstraints(max_length=2000)] | None = None
    style_examples: bool | None = None


# --- stream events (``POST /drafts/generate``) ---------------------------------------------


class _StreamEvent(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


class StartEvent(_StreamEvent):
    """First event: the ID the draft is stored under."""

    type: Literal["start"] = "start"
    draft_id: uuid.UUID


class TokenEvent(_StreamEvent):
    type: Literal["token"] = "token"
    text: str


class DoneEvent(_StreamEvent):
    """Last event on success: the stored draft (with signature)."""

    type: Literal["done"] = "done"
    draft: DraftRead
    ttft_ms: int | None


class ErrorEvent(_StreamEvent):
    """Last event on failure; nothing is stored. Codes: ``llm_unavailable``,
    ``llm_timeout``, ``llm_cloud_disabled``, ``llm_error``, ``internal``."""

    type: Literal["error"] = "error"
    code: str


DraftStreamEvent = Annotated[
    StartEvent | TokenEvent | DoneEvent | ErrorEvent, Field(discriminator="type")
]
