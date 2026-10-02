"""Configured SAML providers (database, admin API).

Providers are read per request instead of once at start-up, so a change in the admin API
takes effect on every API instance immediately.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.providers.base import AuthProvider
from app.auth.providers.saml.config import from_record
from app.auth.providers.saml.models import SAMLProviderRecord
from app.auth.providers.saml.provider import SAMLProvider


async def get_record(db: AsyncSession, name: str) -> SAMLProviderRecord | None:
    return await db.scalar(select(SAMLProviderRecord).where(SAMLProviderRecord.name == name))


async def records(db: AsyncSession) -> list[SAMLProviderRecord]:
    """All providers (also disabled ones), by name."""
    rows = await db.scalars(select(SAMLProviderRecord).order_by(SAMLProviderRecord.name))
    return list(rows)


async def enabled_providers(db: AsyncSession) -> list[AuthProvider]:
    """Source for ``AuthProviderRegistry`` (login page)."""
    return [SAMLProvider(from_record(r)) for r in await records(db) if r.enabled]
