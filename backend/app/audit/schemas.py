"""API schemas for the audit log."""

import uuid
from datetime import datetime

from pydantic import BaseModel

from app.audit.events import ActorKind, AuditAction, TargetType


class AuditEventRead(BaseModel):
    id: int
    occurred_at: datetime
    action: AuditAction
    actor_kind: ActorKind
    actor_id: uuid.UUID | None
    # Current display name of the acting user; null once the user is deleted.
    actor_name: str | None
    target_type: TargetType | None
    target_id: str | None
    # Current display name if the target is a user.
    target_name: str | None
    details: dict[str, str | int | bool | None]


class AuditEventPage(BaseModel):
    items: list[AuditEventRead]
    # Pass as ``before`` to get the next (older) page; null on the last page.
    next_before: int | None


class AuditChainStatus(BaseModel):
    valid: bool
    checked: int
    first_invalid_id: int | None
