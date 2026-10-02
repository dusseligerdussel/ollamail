"""Digest operations: settings, scheduling, generation, audio, feed tokens and cleanup.

Generation runs in two jobs (``app.digest.tasks``): the text on the ``llm`` queue, the
audio on the ``tts`` queue. Both are idempotent: a step that is already done is skipped,
so retries and duplicate jobs do not change the result. No transaction is held open while
the model or the TTS engine works.

Privacy: logs carry IDs, counts and status codes only; never the script, subjects or
senders. Functions take a session; those that hand over to other jobs commit themselves
(noted in the docstring).
"""

import asyncio
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import ColumnElement, and_, any_, delete, exists, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.llm import LLMGateway
from app.ai.tts import AudioFormat, TTSService
from app.core.config import DigestSettings
from app.core.events import Event, publish
from app.core.ids import uuid7
from app.core.logging import get_logger
from app.digest import content as digest_content
from app.digest import schedule, script, texts, tokens
from app.digest.models import (
    ALL_WEEKDAYS,
    Digest,
    DigestLength,
    DigestStatus,
    DigestTrigger,
    DigestUserSettings,
)
from app.digest.storage import DigestStorage
from app.digest.summarize import PROMPT_VERSION, Summarizer
from app.mail.access import accessible_mailbox_ids
from app.mail.models import Mailbox
from app.users.models import User

log = get_logger(__name__)

IN_PROGRESS = (DigestStatus.PENDING, DigestStatus.SUMMARIZING, DigestStatus.SYNTHESIZING)
# Digests stuck in progress for longer are marked failed by the cleanup job.
STALLED_AFTER = timedelta(hours=6)


class DigestNotFoundError(Exception):
    pass


# -- settings ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EffectiveSettings:
    """A user's settings with defaults and profile values filled in."""

    enabled: bool
    delivery_time: time
    timezone: str
    weekdays: list[int]
    language: texts.DigestLanguage
    voice: str | None
    length: DigestLength
    mailbox_ids: list[uuid.UUID] | None

    @property
    def zone(self) -> ZoneInfo:
        return zone(self.timezone)


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def effective(user: User, row: DigestUserSettings | None) -> EffectiveSettings:
    if row is None:
        return EffectiveSettings(
            enabled=False,
            delivery_time=time(7, 0),
            timezone=user.timezone,
            weekdays=list(ALL_WEEKDAYS),
            language=texts.language_of(user.language),
            voice=None,
            length=DigestLength.NORMAL,
            mailbox_ids=None,
        )
    return EffectiveSettings(
        enabled=row.enabled,
        delivery_time=row.delivery_time,
        timezone=row.timezone or user.timezone,
        weekdays=sorted(set(row.weekdays)),
        language=texts.language_of(row.language or user.language),
        voice=row.voice,
        length=row.length,
        mailbox_ids=list(row.mailbox_ids) if row.mailbox_ids is not None else None,
    )


async def settings_row(session: AsyncSession, user_id: uuid.UUID) -> DigestUserSettings | None:
    return await session.scalar(
        select(DigestUserSettings).where(DigestUserSettings.user_id == user_id)
    )


async def ensure_settings_row(session: AsyncSession, user_id: uuid.UUID) -> DigestUserSettings:
    await session.execute(
        insert(DigestUserSettings)
        .values(id=uuid7(), user_id=user_id, delivery_time=time(7, 0), weekdays=ALL_WEEKDAYS)
        .on_conflict_do_nothing(index_elements=[DigestUserSettings.user_id])
    )
    row = await settings_row(session, user_id)
    assert row is not None
    return row


SCHEDULE_FIELDS = frozenset({"enabled", "delivery_time", "timezone", "weekdays"})


async def update_settings(
    session: AsyncSession, user: User, changes: dict[str, Any], *, now: datetime
) -> DigestUserSettings:
    """Apply ``changes`` (only the fields to change). A changed schedule starts with the
    next slot: a slot that already passed today does not fire retroactively."""
    row = await ensure_settings_row(session, user.id)
    for name, value in changes.items():
        setattr(row, name, value)
    if SCHEDULE_FIELDS & changes.keys():
        current = effective(user, row)
        row.last_scheduled_for = schedule.latest_slot(
            now, current.delivery_time, current.zone, current.weekdays
        )
    await session.flush()
    return row


# -- creating digests --------------------------------------------------------------------


async def previous_end(
    session: AsyncSession, user_id: uuid.UUID, before: datetime
) -> datetime | None:
    """End of the user's latest digest that did not fail, ending at or before ``before``."""
    end: datetime | None = await session.scalar(
        select(func.max(Digest.period_end)).where(
            Digest.user_id == user_id,
            Digest.status != DigestStatus.FAILED,
            Digest.period_end <= before,
        )
    )
    return end


async def _new_digest(
    session: AsyncSession,
    user: User,
    trigger: DigestTrigger,
    *,
    period_end: datetime,
    scheduled_for: datetime | None,
    config: DigestSettings,
) -> Digest | None:
    current = effective(user, await settings_row(session, user.id))
    start = schedule.period_start(
        period_end,
        await previous_end(session, user.id, period_end),
        first_lookback=timedelta(hours=config.first_lookback_hours),
        max_lookback=timedelta(days=config.max_lookback_days),
    )
    mailbox_ids = await digest_content.selected_mailboxes(session, user.id, current.mailbox_ids)
    digest_id = await session.scalar(
        insert(Digest)
        .values(
            id=uuid7(),
            user_id=user.id,
            trigger=trigger,
            status=DigestStatus.PENDING,
            scheduled_for=scheduled_for,
            period_start=start,
            period_end=period_end,
            language=current.language,
            voice=current.voice,
            length=current.length,
            mailbox_ids=mailbox_ids,
        )
        .on_conflict_do_nothing(constraint="uq_digests_user_id_scheduled_for")
        .returning(Digest.id)
    )
    if digest_id is None:
        return None
    await publish(session, user.id, Event(type="digest.changed", ids={"digest_id": digest_id}))
    return await session.get(Digest, digest_id)


async def create_manual(
    session: AsyncSession, user: User, *, now: datetime, config: DigestSettings
) -> Digest:
    """A digest for the mails since the last one, now. Does not commit."""
    digest = await _new_digest(
        session, user, DigestTrigger.MANUAL, period_end=now, scheduled_for=None, config=config
    )
    assert digest is not None  # manual digests have no slot to conflict on
    return digest


async def in_progress(session: AsyncSession, user_id: uuid.UUID) -> bool:
    found = await session.scalar(
        select(Digest.id).where(Digest.user_id == user_id, Digest.status.in_(IN_PROGRESS)).limit(1)
    )
    return found is not None


async def schedule_due(
    session: AsyncSession, *, now: datetime, config: DigestSettings
) -> list[uuid.UUID]:
    """Create the digests of all slots that are due; returns their IDs. Commits per user,
    so a slot is marked queued together with its digest."""
    rows = await session.execute(
        select(DigestUserSettings, User)
        .join(User, User.id == DigestUserSettings.user_id)
        .where(DigestUserSettings.enabled, User.is_active)
        .order_by(DigestUserSettings.user_id)
    )
    created: list[uuid.UUID] = []
    for row, user in rows.all():
        current = effective(user, row)
        slot = schedule.due_slot(
            now, current.delivery_time, current.zone, current.weekdays, row.last_scheduled_for
        )
        if slot is None:
            continue
        digest = await _new_digest(
            session,
            user,
            DigestTrigger.SCHEDULED,
            period_end=slot,
            scheduled_for=slot,
            config=config,
        )
        await session.execute(
            update(DigestUserSettings)
            .where(DigestUserSettings.id == row.id)
            .values(last_scheduled_for=slot)
        )
        await session.commit()
        if digest is not None:
            created.append(digest.id)
            log.info("digest_scheduled", user_id=str(user.id), digest_id=str(digest.id))
    return created


# -- generating --------------------------------------------------------------------------


async def _set_status(
    session: AsyncSession,
    digest: Digest,
    status: DigestStatus,
    *,
    error_code: str | None = None,
) -> None:
    digest.status = status
    digest.error_code = error_code
    await publish(
        session,
        digest.user_id,
        Event(type="digest.changed", ids={"digest_id": digest.id}, status=status.value),
    )
    await session.commit()


async def mark_failed(session: AsyncSession, digest_id: uuid.UUID, error_code: str) -> None:
    digest = await session.get(Digest, digest_id)
    if digest is not None and digest.status is not DigestStatus.READY:
        await _set_status(session, digest, DigestStatus.FAILED, error_code=error_code[:64])
        log.warning("digest_failed", digest_id=str(digest_id), error_code=error_code)


async def generate_text(
    session: AsyncSession,
    digest_id: uuid.UUID,
    *,
    llm: LLMGateway,
    config: DigestSettings,
) -> bool:
    """Write the script of a digest. Returns whether audio has to be synthesised next.
    Commits."""
    digest = await session.get(Digest, digest_id)
    if digest is None:
        raise DigestNotFoundError
    if digest.script is not None:
        return digest.status is not DigestStatus.READY
    user = await session.get(User, digest.user_id)
    if user is None:
        raise DigestNotFoundError
    await _set_status(session, digest, DigestStatus.SUMMARIZING)

    current = effective(user, await settings_row(session, user.id))
    today = digest.period_end.astimezone(current.zone).date()
    language = texts.language_of(digest.language)
    collected = await digest_content.collect(
        session,
        user.id,
        digest.mailbox_ids,
        digest.period_start,
        digest.period_end,
        today=today,
        settings=config,
    )
    # End the read transaction before the (slow) model calls.
    await session.commit()

    summarizer = Summarizer(
        llm, config, language=language, length=digest.length, user_label=user.display_name
    )
    summary = await summarizer.summarize(collected.mails)

    digest.title = texts.title(today, language)
    digest.script = script.build(collected, summary.text, today=today, language=language)
    digest.references = [
        {"ref": ref, "message_id": str(mail.message_id), "mailbox_id": str(mail.mailbox_id)}
        for ref, mail in enumerate(collected.mails, start=1)
    ]
    digest.message_count = collected.message_count
    digest.todo_count = collected.todo_count
    digest.model = summary.model
    digest.prompt_version = PROMPT_VERSION if collected.mails else None
    digest.generated_at = datetime.now(UTC)
    needs_audio = config.audio_enabled
    await _set_status(
        session, digest, DigestStatus.SYNTHESIZING if needs_audio else DigestStatus.READY
    )
    log.info(
        "digest_text_ready",
        digest_id=str(digest.id),
        messages=collected.message_count,
        todos=collected.todo_count,
    )
    return needs_audio


async def synthesize_audio(
    session: AsyncSession,
    digest_id: uuid.UUID,
    *,
    tts: TTSService,
    storage: DigestStorage,
    config: DigestSettings,
) -> None:
    """Speak the script and store the audio files. Commits."""
    digest = await session.get(Digest, digest_id)
    if digest is None:
        raise DigestNotFoundError
    if digest.status is DigestStatus.READY or digest.script is None:
        return
    language = texts.language_of(digest.language)
    user_id, voice = digest.user_id, digest.voice
    text = script.spoken(digest.script)
    await session.commit()

    files = await tts.synthesize(
        text,
        lang=language,
        target=storage.target(user_id, digest_id),
        voice=voice,
        formats=[AudioFormat(fmt) for fmt in config.audio_formats],
    )
    digest = await session.get(Digest, digest_id)
    if digest is None:
        # Deleted meanwhile: do not leave the files behind.
        await asyncio.to_thread(storage.delete_digest, user_id, digest_id)
        raise DigestNotFoundError
    digest.audio = {
        file.format.value: {"path": storage.relative(file.path), "size_bytes": file.size_bytes}
        for file in files
    }
    digest.duration_seconds = round(files[0].duration, 2)
    await _set_status(session, digest, DigestStatus.READY)
    log.info("digest_audio_ready", digest_id=str(digest_id), seconds=digest.duration_seconds)


# -- reading and deleting ----------------------------------------------------------------


def readable_by(user_id: uuid.UUID) -> ColumnElement[bool]:
    """Digests of ``user_id`` whose mailboxes the user may all still read. A digest holds
    summaries of mails, so it disappears with the access to any of its mailboxes."""
    mailbox = func.unnest(Digest.mailbox_ids).table_valued("id").render_derived("digest_mailbox")
    unreadable = exists(
        select(mailbox.c.id).where(mailbox.c.id.not_in(accessible_mailbox_ids(user_id)))
    )
    return and_(Digest.user_id == user_id, ~unreadable)


async def list_digests(
    session: AsyncSession, user_id: uuid.UUID, *, limit: int, offset: int
) -> Sequence[Digest]:
    result = await session.scalars(
        select(Digest)
        .where(readable_by(user_id))
        .order_by(Digest.created_at.desc(), Digest.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return result.all()


async def get_digest(
    session: AsyncSession, user_id: uuid.UUID, digest_id: uuid.UUID
) -> Digest | None:
    return await session.scalar(select(Digest).where(Digest.id == digest_id, readable_by(user_id)))


async def delete_digest(session: AsyncSession, digest: Digest, storage: DigestStorage) -> None:
    """Delete a digest, commit, then remove its files."""
    user_id, digest_id = digest.user_id, digest.id
    await session.delete(digest)
    await session.commit()
    await asyncio.to_thread(storage.delete_digest, user_id, digest_id)


# -- podcast feed ------------------------------------------------------------------------


async def rotate_feed_token(session: AsyncSession, user_id: uuid.UUID, *, now: datetime) -> str:
    """A new feed token; the previous one stops working. Does not commit."""
    row = await ensure_settings_row(session, user_id)
    token = tokens.new_token()
    row.feed_token_hash = tokens.hash_token(token)
    row.feed_token_created_at = now
    await session.flush()
    return token


async def revoke_feed_token(session: AsyncSession, user_id: uuid.UUID) -> bool:
    """Disable the feed. Does not commit."""
    result = await session.execute(
        update(DigestUserSettings)
        .where(
            DigestUserSettings.user_id == user_id, DigestUserSettings.feed_token_hash.is_not(None)
        )
        .values(feed_token_hash=None, feed_token_created_at=None)
        .returning(DigestUserSettings.id)
    )
    return result.first() is not None


async def feed_owner(session: AsyncSession, token: str) -> User | None:
    """The active user whose feed token is ``token``."""
    if not tokens.is_well_formed(token):
        return None
    return await session.scalar(
        select(User)
        .join(DigestUserSettings, DigestUserSettings.user_id == User.id)
        .where(DigestUserSettings.feed_token_hash == tokens.hash_token(token), User.is_active)
    )


async def feed_digests(session: AsyncSession, user_id: uuid.UUID, limit: int) -> Sequence[Digest]:
    """Ready digests with audio, newest first."""
    result = await session.scalars(
        select(Digest)
        .where(
            readable_by(user_id),
            Digest.status == DigestStatus.READY,
            Digest.duration_seconds.is_not(None),
        )
        .order_by(Digest.created_at.desc(), Digest.id.desc())
        .limit(limit)
    )
    return result.all()


# -- cleanup -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CleanupResult:
    expired: int = 0
    orphaned: int = 0
    stalled: int = 0
    files: int = 0


async def cleanup(
    session: AsyncSession, storage: DigestStorage, *, now: datetime, retention_days: int
) -> CleanupResult:
    """Delete expired digests and digests of removed mailboxes (with their files), mark
    stalled ones failed and remove files that belong to no digest. Commits."""
    expired_rows = await session.execute(
        delete(Digest)
        .where(Digest.created_at < now - timedelta(days=retention_days))
        .returning(Digest.user_id, Digest.id)
    )
    expired = expired_rows.all()
    existing = (
        select(func.count(Mailbox.id)).where(Mailbox.id == any_(Digest.mailbox_ids))
    ).scalar_subquery()
    orphaned_rows = await session.execute(
        delete(Digest)
        .where(func.cardinality(Digest.mailbox_ids) > existing)
        .returning(Digest.user_id, Digest.id)
    )
    orphaned = orphaned_rows.all()
    stalled = await session.execute(
        update(Digest)
        .where(
            Digest.status.in_(IN_PROGRESS),
            Digest.updated_at < now - STALLED_AFTER,
        )
        .values(status=DigestStatus.FAILED, error_code="stalled")
        .returning(Digest.id)
    )
    stalled_count = len(stalled.all())
    await session.commit()

    for user_id, digest_id in [*expired, *orphaned]:
        await asyncio.to_thread(storage.delete_digest, user_id, digest_id)

    files = 0
    for user_id in await asyncio.to_thread(storage.user_ids):
        if await session.get(User, user_id) is None:
            await asyncio.to_thread(storage.delete_user, user_id)
            files += 1
            continue
        known = set(await session.scalars(select(Digest.id).where(Digest.user_id == user_id)))
        for path in await asyncio.to_thread(storage.orphans, user_id, known):
            await asyncio.to_thread(path.unlink, True)
            files += 1
    await session.commit()
    result = CleanupResult(len(expired), len(orphaned), stalled_count, files)
    if any((result.expired, result.orphaned, result.stalled, result.files)):
        log.info(
            "digest_cleanup",
            expired=result.expired,
            orphaned=result.orphaned,
            stalled=result.stalled,
            files=result.files,
        )
    return result
