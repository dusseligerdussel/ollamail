"""In-memory engine for service tests: one sample per character, nothing is spoken."""

from dataclasses import dataclass, field

from app.ai.tts.errors import VoiceNotAvailableError
from app.ai.tts.types import Language, PCMAudio, VoiceInfo
from app.ai.tts.voices import parse_voice, voice_language

SAMPLE_RATE = 16000


@dataclass
class FakeEngine:
    name: str = "fake"
    installed: set[str] = field(default_factory=set)
    downloadable: bool = True
    calls: list[tuple[str, str, Language]] = field(default_factory=list)
    ensured: list[str] = field(default_factory=list)
    closed: bool = False

    def is_valid_voice(self, voice: str) -> bool:
        return parse_voice(voice) is not None

    def voice_language(self, voice: str) -> Language | None:
        return voice_language(voice)

    def installed_voices(self) -> list[VoiceInfo]:
        return [
            VoiceInfo(voice, self.name, lang, installed=True)
            for voice in sorted(self.installed)
            if (lang := voice_language(voice)) is not None
        ]

    async def ensure_voice(self, voice: str) -> None:
        self.ensured.append(voice)
        if voice not in self.installed:
            if not self.downloadable:
                raise VoiceNotAvailableError("not installed")
            self.installed.add(voice)

    async def synthesize(self, text: str, voice: str, lang: Language) -> PCMAudio:
        self.calls.append((text, voice, lang))
        return PCMAudio(b"\x01\x00" * len(text), SAMPLE_RATE)

    async def aclose(self) -> None:
        self.closed = True
