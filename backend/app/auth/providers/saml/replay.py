"""Replay protection: every assertion ID can sign in once.

Used IDs are kept in Postgres (``auth_saml_assertions``) until the assertion could no
longer be accepted anyway, so all API instances share them. Only a SHA-256 of provider
and assertion ID is stored. Expired rows are removed on the next login.
"""

import hashlib
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.providers.saml.models import assertions

# Never keep an entry longer than this, whatever the assertion says.
MAX_RETENTION = timedelta(days=1)


def _key(provider: str, assertion_id: str) -> bytes:
    return hashlib.sha256(f"{provider}\0{assertion_id}".encode()).digest()


async def remember(
    db: AsyncSession, provider: str, assertion_id: str, valid_until: datetime
) -> bool:
    """Record the assertion; ``False`` if it was used before (replay).

    ``valid_until`` is the last moment the assertion could still be accepted.
    """
    now = datetime.now(UTC)
    expires_at = min(max(valid_until, now), now + MAX_RETENTION)
    await db.execute(delete(assertions).where(assertions.c.expires_at < now))
    inserted = await db.execute(
        insert(assertions)
        .values(key=_key(provider, assertion_id), expires_at=expires_at)
        .on_conflict_do_nothing(index_elements=[assertions.c.key])
        .returning(assertions.c.key)
    )
    return inserted.first() is not None
