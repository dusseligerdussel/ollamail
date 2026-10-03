"""Schemas of the system status for admins (``/admin/system``).

Only states, names of models and endpoints, and counts: never mail content (docs/PRIVACY.md).
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.ai.llm.gateway import ModelState
from app.ai.llm.types import LLMTask
from app.ai.settings.pulls import PullStatus
from app.core.config import LLMProviderKind
from app.mail.api.schemas import SyncPhase
from app.mail.models import MailboxType


class ModelPullRead(BaseModel):
    status: PullStatus
    # Bytes downloaded and expected over all layers seen so far; ``total`` is unknown
    # until Ollama reports the first layer.
    completed: int
    total: int | None
    error_code: str | None
    updated_at: datetime


class ModelStatusRead(BaseModel):
    task: LLMTask
    endpoint: str
    provider: LLMProviderKind
    model: str
    state: ModelState
    # Ollama endpoints can download a missing model (``POST /admin/system/models/pull``).
    can_pull: bool
    # Last download of this model on this endpoint, if any.
    pull: ModelPullRead | None


class ModelPullRequest(BaseModel):
    endpoint: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=255)


class MailboxProcessingRead(BaseModel):
    """Sync status and processing counts of one mailbox; no content, no address."""

    id: uuid.UUID
    type: MailboxType
    # Shared mailboxes only (the admin manages them). A personal mailbox's name defaults to
    # its address, so it is identified by type and owner instead.
    display_name: str | None
    is_shared: bool
    # Display name of the owner; ``None`` for shared mailboxes.
    owner_name: str | None
    sync_phase: SyncPhase
    sync_error: str | None
    processing_enabled: bool
    # Processing steps (not mails) waiting, running and given up.
    pending: int
    running: int
    failed: int


class SystemOverviewRead(BaseModel):
    """Facts for the getting-started checklist and the processing table."""

    mailbox_count: int
    # The signed-in admin's own digest is on, and the instance runs scheduled digests.
    digest_enabled: bool
    digest_scheduler_enabled: bool
    # ``OLLAMAIL_AUTH_PUBLIC_URL`` is set (needed for the podcast feed and OAuth).
    public_url_set: bool
    mailboxes: list[MailboxProcessingRead]


class RetryFailedRead(BaseModel):
    # Messages queued again.
    queued: int
