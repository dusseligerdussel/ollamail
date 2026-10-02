"""Writing, reading and verifying the audit log.

``record`` adds a row in the caller's transaction, so the audit entry is committed (or
rolled back) together with the change it describes. Writers are serialised by a
transaction-level advisory lock to keep the hash chain linear; the lock is held until the
caller commits, which in practice is a few milliseconds.
"""

import hashlib
import json
import re
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, and_, cast, insert, select, text
from sqlalchemy import String as SqlString
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession
from sqlalchemy.orm import aliased

from app.audit.events import Actor, ActorKind, AuditAction, Target, TargetType
from app.audit.models import audit_events
from app.core.logging import is_sensitive
from app.users.models import User

# pg_advisory_xact_lock key for appending to the chain (arbitrary, constant; "auditlog").
CHAIN_LOCK_KEY = 0x61756469746C6F67

MAX_DETAILS = 16
MAX_DETAIL_LENGTH = 128
_DETAIL_KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}$")

DetailValue = str | int | bool | None


class AuditDetailsError(ValueError):
    """Details that could carry personal data or content. A programming error."""


def _clean_details(details: Mapping[str, Any] | None) -> dict[str, DetailValue]:
    """Flat, small, content-free details only: IDs, counts, flags and reason codes.

    Keys the log filter treats as sensitive (``subject``, ``email``, ``to`` ...) are
    rejected, as are strings that look like addresses or free text.
    """
    if not details:
        return {}
    if len(details) > MAX_DETAILS:
        raise AuditDetailsError("too many details")
    cleaned: dict[str, DetailValue] = {}
    for key, value in details.items():
        if not _DETAIL_KEY.match(key) or is_sensitive(key):
            raise AuditDetailsError(f"detail key not allowed: {key!r}")
        if isinstance(value, uuid.UUID):
            value = str(value)
        if isinstance(value, str):
            if len(value) > MAX_DETAIL_LENGTH or "@" in value or "\n" in value:
                raise AuditDetailsError(f"detail value not allowed for {key!r}")
        elif not (value is None or isinstance(value, bool | int)):
            # No floats (no stable JSON form for the hash), no nesting.
            raise AuditDetailsError(f"detail type not allowed for {key!r}")
        cleaned[key] = value
    return cleaned


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def compute_hash(
    prev_hash: bytes | None,
    *,
    occurred_at: datetime,
    action: str,
    actor_kind: str,
    actor_id: uuid.UUID | None,
    target_type: str | None,
    target_id: str | None,
    details: Mapping[str, DetailValue],
) -> bytes:
    """SHA-256 over the predecessor's hash and a canonical JSON form of the row."""
    payload = json.dumps(
        {
            "prev": prev_hash.hex() if prev_hash else None,
            "occurred_at": _timestamp(occurred_at),
            "action": action,
            "actor_kind": actor_kind,
            "actor_id": str(actor_id) if actor_id else None,
            "target_type": target_type,
            "target_id": target_id,
            "details": dict(details),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode()).digest()


async def record(
    db: AsyncSession | AsyncConnection,
    actor: Actor,
    action: AuditAction,
    target: Target | None = None,
    details: Mapping[str, Any] | None = None,
) -> None:
    """Append an audit event in the caller's transaction (caller commits).

    ``details`` must not contain mail content, subjects or addresses; see
    ``_clean_details`` for what is accepted.
    """
    cleaned = _clean_details(details)
    actor_id = actor.user_id if actor.kind is ActorKind.USER else None
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": CHAIN_LOCK_KEY})
    prev_hash = await db.scalar(
        select(audit_events.c.hash).order_by(audit_events.c.id.desc()).limit(1)
    )
    occurred_at = datetime.now(UTC)
    target_type = str(target.type) if target else None
    target_id = target.id if target else None
    digest = compute_hash(
        prev_hash,
        occurred_at=occurred_at,
        action=str(action),
        actor_kind=str(actor.kind),
        actor_id=actor_id,
        target_type=target_type,
        target_id=target_id,
        details=cleaned,
    )
    await db.execute(
        insert(audit_events).values(
            occurred_at=occurred_at,
            action=str(action),
            actor_kind=str(actor.kind),
            actor_id=actor_id,
            target_type=target_type,
            target_id=target_id,
            details=cleaned,
            prev_hash=prev_hash,
            hash=digest,
        )
    )


@dataclass(frozen=True)
class ChainCheck:
    valid: bool
    checked: int
    first_invalid_id: int | None = None


async def verify_chain(db: AsyncSession, batch_size: int = 1000) -> ChainCheck:
    """Recompute every hash in order. The first row may point to a predecessor that was
    removed by retention (#36); from there on every link must match."""
    checked = 0
    expected_prev: bytes | None = None
    last_id = 0
    while True:
        rows = (
            await db.execute(
                select(audit_events)
                .where(audit_events.c.id > last_id)
                .order_by(audit_events.c.id)
                .limit(batch_size)
            )
        ).all()
        if not rows:
            return ChainCheck(valid=True, checked=checked)
        for row in rows:
            if checked > 0 and row.prev_hash != expected_prev:
                return ChainCheck(valid=False, checked=checked, first_invalid_id=row.id)
            digest = compute_hash(
                row.prev_hash,
                occurred_at=row.occurred_at,
                action=row.action,
                actor_kind=row.actor_kind,
                actor_id=row.actor_id,
                target_type=row.target_type,
                target_id=row.target_id,
                details=row.details,
            )
            if digest != row.hash:
                return ChainCheck(valid=False, checked=checked, first_invalid_id=row.id)
            expected_prev = row.hash
            checked += 1
            last_id = row.id


@dataclass(frozen=True)
class AuditFilter:
    action: AuditAction | None = None
    actor_id: uuid.UUID | None = None
    target_type: TargetType | None = None
    target_id: str | None = None
    since: datetime | None = None
    until: datetime | None = None


def _query(filters: AuditFilter) -> Select[Any]:
    """Events, newest first, with the current display names of actor and target user."""
    actor_user = aliased(User)
    target_user = aliased(User)
    query = (
        select(
            audit_events,
            actor_user.display_name.label("actor_name"),
            target_user.display_name.label("target_name"),
        )
        .outerjoin(actor_user, actor_user.id == audit_events.c.actor_id)
        .outerjoin(
            target_user,
            and_(
                audit_events.c.target_type == TargetType.USER.value,
                cast(target_user.id, SqlString) == audit_events.c.target_id,
            ),
        )
        .order_by(audit_events.c.id.desc())
    )
    if filters.action is not None:
        query = query.where(audit_events.c.action == filters.action.value)
    if filters.actor_id is not None:
        query = query.where(audit_events.c.actor_id == filters.actor_id)
    if filters.target_type is not None:
        query = query.where(audit_events.c.target_type == filters.target_type.value)
    if filters.target_id is not None:
        query = query.where(audit_events.c.target_id == filters.target_id)
    if filters.since is not None:
        query = query.where(audit_events.c.occurred_at >= filters.since)
    if filters.until is not None:
        query = query.where(audit_events.c.occurred_at < filters.until)
    return query


async def list_events(
    db: AsyncSession, filters: AuditFilter, *, limit: int, before: int | None = None
) -> list[Any]:
    """One page of events, newest first; ``before`` is the smallest ID of the last page."""
    query = _query(filters).limit(limit)
    if before is not None:
        query = query.where(audit_events.c.id < before)
    return list((await db.execute(query)).all())


async def iter_events(
    db: AsyncSession, filters: AuditFilter, batch_size: int = 1000
) -> AsyncIterator[Any]:
    """All matching events, newest first, read in keyset-paginated batches."""
    before: int | None = None
    while True:
        rows = await list_events(db, filters, limit=batch_size, before=before)
        for row in rows:
            yield row
        if len(rows) < batch_size:
            return
        before = rows[-1].id
