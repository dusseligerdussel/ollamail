"""Fixtures for the notification tests: reuses the triage accounts (synthetic data only)."""

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.crypto import KeyRing, set_keyring
from app.triage.models import TriageCategory


@pytest.fixture(autouse=True)
def _keyring(settings: Settings) -> Iterator[None]:
    """Push subscriptions are encrypted with the process-wide key ring."""
    set_keyring(KeyRing.from_settings(settings.security))
    yield
    set_keyring(None)


async def builtin_category(session: AsyncSession, key: str) -> uuid.UUID:
    """ID of an organisation default category (``important``, ``action_required``, ...)."""
    category_id = await session.scalar(
        select(TriageCategory.id).where(
            TriageCategory.builtin_key == key, TriageCategory.owner_user_id.is_(None)
        )
    )
    assert category_id is not None
    return category_id
