"""Audit log: append-only, hash-chained record of security-relevant events.

Usage (in the transaction of the change, caller commits)::

    from app import audit

    await audit.record(db, audit.Actor.user(user.id), audit.AuditAction.LOGOUT)
"""

from app.audit.events import (
    ANONYMOUS,
    SYSTEM,
    Actor,
    ActorKind,
    AuditAction,
    Target,
    TargetType,
)
from app.audit.service import record

__all__ = [
    "ANONYMOUS",
    "SYSTEM",
    "Actor",
    "ActorKind",
    "AuditAction",
    "Target",
    "TargetType",
    "record",
]
