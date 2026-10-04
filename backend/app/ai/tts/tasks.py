"""Jobs on the ``tts`` queue.

Synthesis jobs belong to the features that own the text (the daily digest, #28); they
call :func:`app.ai.tts.service.get_tts` inside their own ``queue="tts"`` task, with IDs
as arguments only. This module keeps the default and allowlisted voices installed, so the
first digest does not wait for a download.
"""

from app.ai.tts.errors import TTSError
from app.ai.tts.service import TTS_QUEUE, get_tts
from app.core.logging import get_logger
from app.worker import DEFAULT_RETRY, app

log = get_logger(__name__)


@app.periodic(cron="23 4 * * *", periodic_id="tts_ensure_voices")
@app.task(
    name="tts.ensure_voices",
    queue=TTS_QUEUE,
    queueing_lock="tts.ensure_voices",
    retry=DEFAULT_RETRY,
)
async def ensure_voices(timestamp: int) -> None:
    """Download missing default and allowlisted voices (no-op when installed or downloads
    are off)."""
    tts = get_tts()
    if not tts.settings.download_voices:
        return
    try:
        await tts.ensure_offered_voices()
    except TTSError as exc:
        log.warning("tts_ensure_voices_failed", error_type=type(exc).__name__)
        raise
