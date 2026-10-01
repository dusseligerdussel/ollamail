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

    def resolve_voice(self, lang: Language, preferred: str | None = None) -> str:
        """The user's voice if it is valid for ``lang``, otherwise the default voice."""
        if (
            preferred
            and self.engine.is_valid_voice(preferred)
            and self.engine.voice_language(preferred) == lang
        ):
            return preferred
        return self.default_voice(lang)

    def voices(self, lang: Language | None = None) -> list[VoiceInfo]:
        """Voices to offer for selection: installed ones plus the (downloadable) defaults."""
        found = {voice.id: voice for voice in self.engine.installed_voices()}
        for default_lang in LANGUAGES:
            voice = self.default_voice(default_lang)
            if voice not in found and self.engine.voice_language(voice) == default_lang:
                found[voice] = VoiceInfo(voice, self.engine.name, default_lang, installed=False)
        voices = sorted(found.values(), key=lambda v: (v.lang, v.id))
        return [voice for voice in voices if lang is None or voice.lang == lang]

    async def ensure_default_voices(self) -> None:
        for lang in LANGUAGES:
            await self.engine.ensure_voice(self.default_voice(lang))

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
