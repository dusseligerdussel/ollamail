"""The audit log table (docs/PRIVACY.md, "Audit-Log").

Append-only: a database trigger (migration ``add_audit_events``) rejects ``UPDATE``,
``DELETE`` and ``TRUNCATE``. Every row carries the SHA-256 of its predecessor
(``prev_hash``) and of itself (``hash``), so removing or changing a row breaks the chain
(``app.audit.service.verify_chain``).

There are deliberately no foreign keys: deleting a user or mailbox must not delete or
change audit rows. IDs are pseudonymous references; names are resolved at read time and
disappear with the user. No mail content, subjects or addresses are stored.
"""

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Identity,
    Index,
    LargeBinary,
    String,
    Table,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.core.db import Base

audit_events = Table(
    "audit_events",
    Base.metadata,
    # Sequential instead of UUIDv7: gaps are visible and the order defines the hash chain.
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("occurred_at", DateTime(timezone=True), nullable=False, index=True),
    Column("action", String(64), nullable=False, index=True),
    # "user", "system" or "anonymous"; actor_id is set for "user" only.
    Column("actor_kind", String(16), nullable=False),
    Column("actor_id", Uuid, nullable=True, index=True),
    Column("target_type", String(32), nullable=True),
    Column("target_id", String(64), nullable=True),
    # Small, flat key/value pairs (IDs, counts, flags, reason codes); see ``record``.
    Column("details", JSONB, nullable=False),
    Column("prev_hash", LargeBinary(32), nullable=True),
    Column("hash", LargeBinary(32), nullable=False, unique=True),
    Index("ix_audit_events_target", "target_type", "target_id"),
)
