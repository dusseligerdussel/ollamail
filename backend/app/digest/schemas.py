"""API schemas for digests, digest settings and the podcast feed."""

import uuid
from datetime import datetime, time
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

from app.digest.models import DigestLength, DigestStatus, DigestTrigger
from app.users.schemas import TimeZone

DigestLanguage = Literal["de", "en"]
AudioFormatName = Literal["mp3", "opus"]
VoiceId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,128}$")]


def _weekdays(value: list[int]) -> list[int]:
    return sorted(set(value))


def _minutes(value: time) -> time:
    # Slots have minute resolution; the time zone is a separate setting.
    return value.replace(second=0, microsecond=0, tzinfo=None)


Weekdays = Annotated[
    list[Annotated[int, Field(ge=0, le=6)]],
    Field(max_length=7, description="0 = Monday ... 6 = Sunday"),
    AfterValidator(_weekdays),
]
DeliveryTime = Annotated[time, AfterValidator(_minutes)]


class FeedStatus(BaseModel):
    """Whether the podcast feed is enabled. The token itself is only shown on creation."""

    active: bool
    created_at: datetime | None


class FeedCreated(BaseModel):
    # Secret: anyone with this URL can read the feed and listen to the digests.
    feed_url: str
    created_at: datetime


class DigestSettingsRead(BaseModel):
    enabled: bool
    delivery_time: time
    # ``null``: the profile's time zone (``effective_timezone``).
    timezone: str | None
    effective_timezone: str
    weekdays: list[int]
    # ``null``: the profile's language (``effective_language``).
    language: DigestLanguage | None
    effective_language: DigestLanguage
    # ``null``: the default voice of the language.
    voice: str | None
    length: DigestLength
    # ``null``: all mailboxes.
    mailbox_ids: list[uuid.UUID] | None
    # Next scheduled digest (UTC), ``null`` if disabled.
    next_run_at: datetime | None
    feed: FeedStatus


class DigestSettingsUpdate(BaseModel):
    """Fields to change; omitted fields stay. ``null`` resets timezone, language, voice and
    mailboxes to their defaults."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    delivery_time: DeliveryTime | None = None
    timezone: TimeZone | None = None
    weekdays: Weekdays | None = None
    language: DigestLanguage | None = None
    voice: VoiceId | None = None
    length: DigestLength | None = None
    mailbox_ids: Annotated[list[uuid.UUID], Field(max_length=100)] | None = None


class DigestReference(BaseModel):
    """``[ref]`` in the script refers to this mail."""

    ref: int
    message_id: uuid.UUID
    mailbox_id: uuid.UUID


class DigestSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    trigger: DigestTrigger
    status: DigestStatus
    error_code: str | None
    title: str
    language: str
    length: DigestLength
    # Mails received in [period_start, period_end) are covered.
    period_start: datetime
    period_end: datetime
    scheduled_for: datetime | None
    message_count: int
    todo_count: int
    duration_seconds: float | None
    audio_formats: list[AudioFormatName]
    created_at: datetime
    generated_at: datetime | None


class DigestRead(DigestSummary):
    # Markdown with ``[n]`` references; ``null`` until the text is written.
    script: str | None
    references: list[DigestReference]
    voice: str | None
    model: str | None
