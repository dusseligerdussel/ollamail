"""Digest jobs.

* ``digest.schedule`` (periodic, every minute, queue ``default``): creates the digests whose
  slot is due and queues them.
* ``digest.generate`` (queue ``llm``): writes the script (map-reduce summary).
* ``digest.synthesize`` (queue ``tts``): speaks the script into MP3/Opus files.
* ``digest.cleanup`` (periodic, hourly, queue ``default``): retention and file cleanup.

Job arguments are digest IDs only. ``lock`` serialises the jobs of one digest.
"""

import contextlib
import uuid
from datetime import UTC, datetime

from procrastinate.exceptions import AlreadyEnqueued

from app.ai.llm import CloudLLMDisabledError, LLMGateway
from app.ai.settings.runtime import worker_gateway
from app.ai.tts import TTS_QUEUE, VoiceNotAvailableError, get_tts
from app.core.config import get_settings
from app.core.logging import get_logger
from app.digest import service
from app.digest.storage import DigestStorage
from app.processing.tasks import error_code, get_database
from app.worker import DEFAULT_RETRY, app, resource_lock

log = get_logger(__name__)

_llm: LLMGateway | None = None


def get_llm() -> LLMGateway:
    """LLM gateway of the worker process, created on first use."""
    global _llm
    if _llm is None:
        # Shared by all jobs of the process: admin settings, limited parallelism.
        _llm = worker_gateway()
    return _llm


def get_storage() -> DigestStorage:
    return DigestStorage(get_settings().storage.data_dir)


def _lock(digest_id: uuid.UUID | str) -> str:
    return resource_lock("digest", digest_id)


async def enqueue_generation(digest_id: uuid.UUID) -> None:
    """Queue the text job of a committed digest; a second call while it waits is a no-op."""
    with contextlib.suppress(AlreadyEnqueued):
        await generate_digest.configure(
            lock=_lock(digest_id), queueing_lock=f"{_lock(digest_id)}:text"
        ).defer_async(digest_id=str(digest_id))


async def enqueue_synthesis(digest_id: uuid.UUID) -> None:
    with contextlib.suppress(AlreadyEnqueued):
        await synthesize_digest.configure(
            lock=_lock(digest_id), queueing_lock=f"{_lock(digest_id)}:audio"
        ).defer_async(digest_id=str(digest_id))


@app.periodic(cron="* * * * *", periodic_id="digest_schedule")
@app.task(
    name="digest.schedule",
    queue="default",
    queueing_lock="digest.schedule",
    lock="digest.schedule",
    retry=DEFAULT_RETRY,
)
async def schedule_digests(timestamp: int) -> None:
    """Every minute: create and queue the digests whose slot has come."""
    config = get_settings().digest
    if not config.enabled:
        return
    now = datetime.fromtimestamp(timestamp, UTC)
    async with get_database().sessionmaker() as session:
        created = await service.schedule_due(session, now=now, config=config)
    for digest_id in created:
        await enqueue_generation(digest_id)


@app.task(name="digest.generate", queue="llm", retry=DEFAULT_RETRY)
async def generate_digest(digest_id: str) -> None:
    config = get_settings().digest
    async with get_database().sessionmaker() as session:
        try:
            needs_audio = await service.generate_text(
                session, uuid.UUID(digest_id), llm=get_llm(), config=config
            )
        except service.DigestNotFoundError:
            return
        except CloudLLMDisabledError:
            # Permanent until the admin changes the configuration: no retries.
            await session.rollback()
            await service.mark_failed(session, uuid.UUID(digest_id), "llm_cloud_disabled")
            return
        except Exception as exc:
            await session.rollback()
            await service.mark_failed(session, uuid.UUID(digest_id), error_code(exc))
            raise
    if needs_audio:
        await enqueue_synthesis(uuid.UUID(digest_id))


@app.task(name="digest.synthesize", queue=TTS_QUEUE, retry=DEFAULT_RETRY)
async def synthesize_digest(digest_id: str) -> None:
    config = get_settings().digest
    async with get_database().sessionmaker() as session:
        try:
            await service.synthesize_audio(
                session,
                uuid.UUID(digest_id),
                tts=get_tts(),
                storage=get_storage(),
                config=config,
            )
        except service.DigestNotFoundError:
            return
        except VoiceNotAvailableError:
            await session.rollback()
            await service.mark_failed(session, uuid.UUID(digest_id), "tts_voice_not_available")
            return
        except Exception as exc:
            await session.rollback()
            await service.mark_failed(session, uuid.UUID(digest_id), error_code(exc))
            raise


@app.periodic(cron="41 * * * *", periodic_id="digest_cleanup")
@app.task(
    name="digest.cleanup",
    queue="default",
    queueing_lock="digest.cleanup",
    lock="digest.cleanup",
    retry=DEFAULT_RETRY,
)
async def cleanup_digests(timestamp: int) -> None:
    """Hourly: delete expired digests (retention) and files without a digest."""
    settings = get_settings()
    async with get_database().sessionmaker() as session:
        await service.cleanup(
            session,
            get_storage(),
            now=datetime.fromtimestamp(timestamp, UTC),
            retention_days=settings.digest.retention_days,
        )
