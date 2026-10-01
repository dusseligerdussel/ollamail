"""The engine interface (docs/ARCHITECTURE.md §3.3).

Engines only turn one short, already normalised piece of text into PCM audio and manage
their voices. Text normalisation, splitting long texts, pauses, concatenation and
encoding live in :class:`app.ai.tts.service.TTSService`, so new engines get them for free.
"""

from typing import Protocol

from app.ai.tts.types import Language, PCMAudio, VoiceInfo


class TTSEngine(Protocol):
    name: str

    def is_valid_voice(self, voice: str) -> bool:
        """Whether ``voice`` is a well-formed voice ID of this engine (not if it exists)."""
        ...

    def voice_language(self, voice: str) -> Language | None:
        """Language a voice speaks, ``None`` if unsupported or unknown."""
        ...

    def installed_voices(self) -> list[VoiceInfo]:
        """Voices that can be used without a download."""
        ...

    async def ensure_voice(self, voice: str) -> None:
        """Make ``voice`` available (download it if allowed); idempotent.

        Raises ``VoiceNotAvailableError`` or ``VoiceDownloadError``."""
        ...

    async def synthesize(self, text: str, voice: str, lang: Language) -> PCMAudio:
        """Speak one piece of normalised text (at most a few sentences)."""
        ...

    async def aclose(self) -> None: ...
