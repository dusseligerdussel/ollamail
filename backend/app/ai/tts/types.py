from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal, get_args

# Languages with text normalisation and a default voice.
Language = Literal["de", "en"]
LANGUAGES: tuple[Language, ...] = get_args(Language)


class AudioFormat(StrEnum):
    OPUS = "opus"
    MP3 = "mp3"

    @property
    def extension(self) -> str:
        return f".{self.value}"

    @property
    def media_type(self) -> str:
        return "audio/ogg" if self is AudioFormat.OPUS else "audio/mpeg"


@dataclass(frozen=True)
class PCMAudio:
    """Raw audio: signed 16-bit little-endian samples, mono."""

    samples: bytes
    sample_rate: int

    @property
    def duration(self) -> float:
        return len(self.samples) / 2 / self.sample_rate


@dataclass(frozen=True)
class AudioFile:
    path: Path
    format: AudioFormat
    duration: float
    size_bytes: int


@dataclass(frozen=True)
class VoiceInfo:
    id: str
    engine: str
    lang: Language
    installed: bool
