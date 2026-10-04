"""Fixtures for the notification tests: reuses the triage accounts (synthetic data only)."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.triage.models import TriageCategory


async def builtin_category(session: AsyncSession, key: str) -> uuid.UUID:
    """ID of an organisation default category (``important``, ``action_required``, ...)."""
    category_id = await session.scalar(
        select(TriageCategory.id).where(
            TriageCategory.builtin_key == key, TriageCategory.owner_user_id.is_(None)
        )
    )
    assert category_id is not None
    return category_id
