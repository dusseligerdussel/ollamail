"""Effective retention periods: the admin's values, else the environment.

Read by the jobs that enforce them (``privacy.retention``, ``digest.cleanup``,
``rag.purge_conversations``) at every run, so a change applies without a restart.
"""

from dataclasses import asdict, dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.privacy.models import RetentionSettingsRecord


@dataclass(frozen=True)
class RetentionPolicy:
    """Days per data category; 0 keeps the data (digests are always deleted, min. 1)."""

    mail_days: int
    attachment_days: int
    search_index_days: int
    rag_history_days: int
    digest_days: int
    audit_days: int

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


def default_policy(settings: Settings) -> RetentionPolicy:
    """The values from the environment."""
    return RetentionPolicy(
        mail_days=settings.privacy.mail_retention_days,
        attachment_days=settings.privacy.attachment_retention_days,
        search_index_days=settings.privacy.search_index_retention_days,
        rag_history_days=settings.rag.history_retention_days,
        digest_days=settings.digest.retention_days,
        audit_days=settings.audit.retention_days,
    )


def merge(defaults: RetentionPolicy, record: RetentionSettingsRecord | None) -> RetentionPolicy:
    if record is None:
        return defaults
    values = defaults.as_dict()
    for name in values:
        stored = getattr(record, name)
        if stored is not None:
            values[name] = stored
    return RetentionPolicy(**values)


async def get_record(session: AsyncSession) -> RetentionSettingsRecord | None:
    return await session.scalar(select(RetentionSettingsRecord))


async def effective_policy(session: AsyncSession, settings: Settings) -> RetentionPolicy:
    return merge(default_policy(settings), await get_record(session))
