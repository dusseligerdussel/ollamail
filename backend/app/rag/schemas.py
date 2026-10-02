"""API schemas of "ask your inbox": request, stream events and stored conversations."""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.rag.models import AnswerStatus, RagRole

FilterField = Literal["mailbox_ids", "category_ids", "sender", "since", "until"]


class RagFilters(BaseModel):
    """Filters set in the UI; each one narrows the mailboxes the user may read."""

    model_config = ConfigDict(extra="forbid")

    mailbox_ids: list[uuid.UUID] | None = Field(default=None, max_length=100)
    folder_ids: list[uuid.UUID] | None = Field(default=None, max_length=100)
    category_ids: list[uuid.UUID] | None = Field(default=None, max_length=100)
    # Part of the sender's name or address.
    sender: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None
    # Date of the mail: ``since <= date < until``.
    since: datetime | None = None
    until: datetime | None = None


class AppliedFilters(RagFilters):
    """Filters the search used: the UI filters, completed by those found in the question."""

    model_config = ConfigDict(extra="ignore")

    # Filters taken from the question (not set in the UI).
    extracted: list[FilterField] = Field(default_factory=list)


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
    ]
    # Continue a conversation (follow-up question); omitted: start a new one.
    conversation_id: uuid.UUID | None = None
    filters: RagFilters = Field(default_factory=RagFilters)


class Source(BaseModel):
    """A retrieved chunk, passed to the model as source ``[number]``."""

    number: int
    message_id: uuid.UUID
    mailbox_id: uuid.UUID
    attachment_id: uuid.UUID | None
    # ``body``, ``attachment`` or ``attachment_ocr`` (text recognised from a scan).
    source: str
    # Header context of the chunk (sender, date, subject).
    heading: str
    # Excerpt of the chunk.
    snippet: str


# --- stream events (``POST /rag/ask``) -------------------------------------------------


class _StreamEvent(BaseModel):
    # ``type`` is always sent: required in the (serialization) schema.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


class StartEvent(_StreamEvent):
    """First event: IDs of the conversation and of the question and answer to be stored."""

    type: Literal["start"] = "start"
    conversation_id: uuid.UUID
    question_id: uuid.UUID
    answer_id: uuid.UUID


class FiltersEvent(_StreamEvent):
    type: Literal["filters"] = "filters"
    filters: AppliedFilters


class SourcesEvent(_StreamEvent):
    """The numbered sources the model answers from; citations refer to their numbers."""

    type: Literal["sources"] = "sources"
    sources: list[Source]


class TokenEvent(_StreamEvent):
    """Next piece of the answer. Citation markers ``[n]`` only name listed sources."""

    type: Literal["token"] = "token"
    text: str


class DoneEvent(_StreamEvent):
    """Last event of a successful answer; it is stored in the conversation."""

    type: Literal["done"] = "done"
    status: AnswerStatus
    # Numbers of the sources the answer cites, in order of first citation.
    citations: list[int]
    # Milliseconds from the request to the first piece of the answer.
    ttft_ms: int | None


class ErrorEvent(_StreamEvent):
    """Last event if no answer could be generated; nothing is stored. Codes:
    ``llm_unavailable``, ``llm_timeout``, ``llm_cloud_disabled``, ``llm_error``, ``internal``."""

    type: Literal["error"] = "error"
    code: str


RagStreamEvent = Annotated[
    StartEvent | FiltersEvent | SourcesEvent | TokenEvent | DoneEvent | ErrorEvent,
    Field(discriminator="type"),
]


# --- stored conversations --------------------------------------------------------------


class ConversationSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    created_at: datetime
    updated_at: datetime


class CitationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    number: int
    message_id: uuid.UUID
    mailbox_id: uuid.UUID
    attachment_id: uuid.UUID | None
    source: str
    heading: str
    snippet: str


class ConversationMessage(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: RagRole
    content: str
    # Answer written from a mailbox the user can no longer read: ``content`` is empty.
    withheld: bool = False
    created_at: datetime
    # Question: filters the search used.
    filters: AppliedFilters | None
    # Answer: status and the cited sources that still exist and are readable.
    status: AnswerStatus | None
    citations: list[CitationRead]


class ConversationRead(ConversationSummary):
    messages: list[ConversationMessage]
