"""Entry point for features: ``await tts.synthesize(text, lang="de", target=...)``.

The service picks the voice (user choice or default per language), normalises and
segments the text, lets the engine speak it piece by piece, inserts pauses and streams
the result into the encoder. Call it from jobs on the ``tts`` queue (see
:data:`TTS_QUEUE`), never from a request handler: synthesis is CPU-bound.

Privacy: the text is never logged; logs carry voice, language, sizes and timings only.
"""

import time
from collections.abc import AsyncIterator, Sequence
from functools import lru_cache
from pathlib import Path

from app.ai.tts.base import TTSEngine
from app.ai.tts.encode import FFmpegEncoder
from app.ai.tts.errors import SynthesisError
from app.ai.tts.normalize import Segment, segment
from app.ai.tts.registry import create_engine
from app.ai.tts.types import LANGUAGES, AudioFile, AudioFormat, Language, VoiceInfo
from app.core.config import Settings, TTSSettings, get_settings
from app.core.logging import get_logger

log = get_logger(__name__)

TTS_QUEUE = "tts"
DEFAULT_FORMATS: tuple[AudioFormat, ...] = (AudioFormat.OPUS,)


class TTSService:
    def __init__(self, engine: TTSEngine, encoder: FFmpegEncoder, settings: TTSSettings) -> None:
        self.engine = engine
        self.encoder = encoder
        self.settings = settings

    def default_voice(self, lang: Language) -> str:
        return self.settings.voice_de if lang == "de" else self.settings.voice_en

    def offered_voices(self) -> list[str]:
        """Voices the admin offers besides the installed ones: the default voice per
        language plus ``OLLAMAIL_TTS_VOICE_ALLOWLIST``. Only these are ever downloaded."""
        voices = [self.default_voice(lang) for lang in LANGUAGES]
        voices += [v for v in self.settings.voice_allowlist if v not in voices]
        return [v for v in voices if self.engine.voice_language(v) in LANGUAGES]

    def is_available(self, voice: str) -> bool:
        """Whether users may pick ``voice``: offered by the admin or already installed.
        Any other voice ID, however valid, is never downloaded on a user's behalf (#191)."""
        if not self.engine.is_valid_voice(voice):
            return False
        if voice in self.offered_voices():
            return True
        return any(installed.id == voice for installed in self.engine.installed_voices())

    def resolve_voice(self, lang: Language, preferred: str | None = None) -> str:
        """The user's voice if it is available and speaks ``lang``, otherwise the default."""
        if (
            preferred
            and self.engine.voice_language(preferred) == lang
            and self.is_available(preferred)
        ):
            return preferred
        return self.default_voice(lang)

    def voices(self, lang: Language | None = None) -> list[VoiceInfo]:
        """Voices to offer for selection: installed ones plus the offered (downloadable)
        defaults and allowlisted voices."""
        found = {voice.id: voice for voice in self.engine.installed_voices()}
        for voice in self.offered_voices():
            voice_lang = self.engine.voice_language(voice)
            if voice not in found and voice_lang is not None:
                found[voice] = VoiceInfo(voice, self.engine.name, voice_lang, installed=False)
        voices = sorted(found.values(), key=lambda v: (v.lang, v.id))
        return [voice for voice in voices if lang is None or voice.lang == lang]

    async def ensure_offered_voices(self) -> None:
        """Download missing default and allowlisted voices (worker, ``tts`` queue)."""
        for voice in self.offered_voices():
            await self.engine.ensure_voice(voice)

    def segments(self, text: str, lang: Language) -> list[Segment]:
        return segment(
            text,
            lang,
            max_chars=self.settings.max_chunk_chars,
            sentence_pause=self.settings.sentence_pause,
            paragraph_pause=self.settings.paragraph_pause,
        )

    async def synthesize(
        self,
        text: str,
        *,
        lang: Language,
        target: Path,
        voice: str | None = None,
        formats: Sequence[AudioFormat] = DEFAULT_FORMATS,
    ) -> list[AudioFile]:
        """Speak ``text`` and write ``<target>.<ext>`` per format (``target`` without
        extension, e.g. ``<data_dir>/digests/<user_id>/<digest_id>``)."""
        voice = self.resolve_voice(lang, voice)
        segments = self.segments(text, lang)
        if not segments:
            raise ValueError("nothing to speak")
        await self.engine.ensure_voice(voice)

        started = time.perf_counter()
        pcm = self._pcm(segments, voice, lang)
        # The sample rate is known once the first piece is spoken.
        first, sample_rate = await anext(pcm)

        async def stream() -> AsyncIterator[bytes]:
            yield first
            async for chunk, rate in pcm:
                if rate != sample_rate:
                    raise SynthesisError("voice changed its sample rate")
                yield chunk

        files = await self.encoder.encode(
            stream(), sample_rate=sample_rate, target=target, formats=formats
        )
        elapsed = time.perf_counter() - started
        duration = files[0].duration
        log.info(
            "tts_synthesized",
            engine=self.engine.name,
            voice=voice,
            lang=lang,
            segments=len(segments),
            chars=sum(len(s.text) for s in segments),
            audio_seconds=round(duration, 1),
            elapsed_seconds=round(elapsed, 2),
            real_time_factor=round(elapsed / duration, 3) if duration else None,
            formats=[f.format.value for f in files],
        )
        return files

    async def _pcm(
        self, segments: list[Segment], voice: str, lang: Language
    ) -> AsyncIterator[tuple[bytes, int]]:
        for piece in segments:
            audio = await self.engine.synthesize(piece.text, voice, lang)
            silence = b"\0\0" * int(audio.sample_rate * piece.pause_after)
            yield audio.samples + silence, audio.sample_rate

    async def aclose(self) -> None:
        await self.engine.aclose()


def create_tts(settings: Settings) -> TTSService:
    tts = settings.tts
    encoder = FFmpegEncoder(
        tts.ffmpeg_path,
        opus_bitrate_kbps=tts.opus_bitrate_kbps,
        mp3_bitrate_kbps=tts.mp3_bitrate_kbps,
    )
    return TTSService(create_engine(settings), encoder, tts)


@lru_cache
def get_tts() -> TTSService:
    """The process-wide service (one engine, so loaded voices are shared)."""
    return create_tts(get_settings())
