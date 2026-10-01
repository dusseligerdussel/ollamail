"""Piper engine (ADR 4): fast neural TTS on CPU, including ARM.

Piper runs in-process (``piper-tts``, ONNX Runtime). Loaded voices are cached, because
loading takes about a second. Synthesis runs in a thread and is serialised per engine:
Piper's espeak-ng phonemizer is not thread-safe, and ONNX Runtime already uses all cores
for one utterance, so parallel jobs would only compete for the CPU.
"""

import asyncio
import importlib.util
import os
import threading
from collections import OrderedDict
from typing import TYPE_CHECKING

from app.ai.tts.errors import SynthesisError, VoiceNotAvailableError
from app.ai.tts.types import Language, PCMAudio, VoiceInfo
from app.ai.tts.voices import PiperVoiceStore, parse_voice, voice_language
from app.core.config import Settings
from app.core.logging import get_logger

if TYPE_CHECKING:
    from piper import PiperVoice

log = get_logger(__name__)

# ONNX Runtime (>= 1.2x) ships a Microsoft telemetry client that is enabled by default on
# Linux, too: it creates a device ID under ~/.cache/Microsoft and queues usage events for
# upload. ollamail has no telemetry (docs/PRIVACY.md), so it is switched off before ONNX
# Runtime is loaded; the image sets the same variable.
os.environ["ORT_DISABLE_TELEMETRY"] = "1"

# Voices kept in memory per process (a medium voice needs ~100 MB).
MAX_LOADED_VOICES = 2


def piper_available() -> bool:
    return importlib.util.find_spec("piper") is not None


class PiperEngine:
    name = "piper"

    def __init__(
        self,
        store: PiperVoiceStore,
        *,
        sentence_pause: float = 0.35,
        length_scale: float | None = None,
    ) -> None:
        self.store = store
        self.sentence_pause = sentence_pause
        self.length_scale = length_scale
        self._voices: OrderedDict[str, PiperVoice] = OrderedDict()
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings: Settings) -> "PiperEngine":
        tts = settings.tts
        store = PiperVoiceStore(
            settings.storage.data_dir,
            base_url=tts.voice_base_url,
            download=tts.download_voices,
            timeout=tts.download_timeout,
        )
        return cls(store, sentence_pause=tts.sentence_pause, length_scale=tts.length_scale)

    def is_valid_voice(self, voice: str) -> bool:
        return parse_voice(voice) is not None

    def voice_language(self, voice: str) -> Language | None:
        return voice_language(voice)

    def installed_voices(self) -> list[VoiceInfo]:
        return self.store.installed()

    async def ensure_voice(self, voice: str) -> None:
        await self.store.ensure(voice)

    def _load(self, voice: str) -> "PiperVoice":
        # Called with self._lock held.
        loaded = self._voices.get(voice)
        if loaded is not None:
            self._voices.move_to_end(voice)
            return loaded
        if not self.store.is_installed(voice):
            raise VoiceNotAvailableError(f"voice {voice} is not installed")
        from piper import PiperVoice

        loaded = PiperVoice.load(
            self.store.model_path(voice),
            config_path=self.store.config_path(voice),
            download_dir=self.store.root,
        )
        self._voices[voice] = loaded
        while len(self._voices) > MAX_LOADED_VOICES:
            self._voices.popitem(last=False)
        log.info("tts_voice_loaded", voice=voice)
        return loaded

    def _synthesize(self, text: str, voice: str) -> PCMAudio:
        from piper import SynthesisConfig

        with self._lock:
            piper_voice = self._load(voice)
            config = SynthesisConfig(length_scale=self.length_scale)
            sample_rate = piper_voice.config.sample_rate
            silence = b"\0\0" * int(sample_rate * self.sentence_pause)
            parts: list[bytes] = []
            # Piper splits the text into sentences itself and yields one chunk per sentence.
            for chunk in piper_voice.synthesize(text, syn_config=config):
                if parts:
                    parts.append(silence)
                parts.append(chunk.audio_int16_bytes)
                sample_rate = chunk.sample_rate
        return PCMAudio(b"".join(parts), sample_rate)

    async def synthesize(self, text: str, voice: str, lang: Language) -> PCMAudio:
        if voice_language(voice) != lang:
            raise VoiceNotAvailableError("voice does not speak the requested language")
        try:
            return await asyncio.to_thread(self._synthesize, text, voice)
        except VoiceNotAvailableError:
            raise
        except Exception as exc:
            raise SynthesisError("piper failed to synthesise") from exc

    async def aclose(self) -> None:
        with self._lock:
            self._voices.clear()
