"""Fixtures for audit tests; sign-in helpers and scratch databases come from the auth tests."""

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.models import audit_events
from app.mail.storage import AttachmentStorage
from tests.auth.conftest import _cheap_hashing, scratch_database  # noqa: F401


async def audit_rows(db: AsyncSession, action: str | None = None) -> list[Any]:
    """Audit rows in insertion order, optionally of one action."""
    query = select(audit_events).order_by(audit_events.c.id)
    if action is not None:
        query = query.where(audit_events.c.action == action)
    return list((await db.execute(query)).all())


@pytest.fixture
def storage(tmp_path: Path) -> AttachmentStorage:
    return AttachmentStorage(tmp_path)
