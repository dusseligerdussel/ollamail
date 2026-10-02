"""Configured GitHub providers (database, admin API).

Providers are read per request instead of once at start-up, so a change in the admin API
takes effect on every API instance immediately.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.providers.base import AuthProvider
from app.auth.providers.github.config import GitHubConfig, from_record
from app.auth.providers.github.models import GitHubProviderRecord
from app.auth.providers.github.provider import GitHubProvider


async def get_record(db: AsyncSession, name: str) -> GitHubProviderRecord | None:
    return await db.scalar(select(GitHubProviderRecord).where(GitHubProviderRecord.name == name))


async def records(db: AsyncSession) -> list[GitHubProviderRecord]:
    """All providers (also disabled ones), by name."""
    rows = await db.scalars(select(GitHubProviderRecord).order_by(GitHubProviderRecord.name))
    return list(rows)


async def configs(db: AsyncSession) -> list[GitHubConfig]:
    return [from_record(r) for r in await records(db)]


async def enabled_provider(db: AsyncSession, name: str) -> GitHubProvider | None:
    record = await get_record(db, name)
    if record is None or not record.enabled:
        return None
    return GitHubProvider(from_record(record))


async def enabled_providers(db: AsyncSession) -> list[AuthProvider]:
    """Source for ``AuthProviderRegistry`` (login page)."""
    return [GitHubProvider(c) for c in await configs(db) if c.enabled]
