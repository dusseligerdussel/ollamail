import io
import json
import os
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from app.ai.tts.encode import FFmpegEncoder
from app.ai.tts.errors import EncodingError, VoiceNotAvailableError
from app.ai.tts.service import TTSService, create_tts
from app.ai.tts.types import AudioFile, AudioFormat, VoiceInfo
from app.core.config import LoggingSettings, Settings, StorageSettings, TTSSettings
from app.core.logging import configure_logging
from tests.ai.tts.conftest import requires_ffmpeg
from tests.ai.tts.fakes import SAMPLE_RATE, FakeEngine

DE = "de_DE-thorsten-medium"
EN = "en_US-ljspeech-medium"
SECRET = "Vertrauliche Gehaltsabrechnung von Anna Berg"


@dataclass
class RecordingEncoder(FFmpegEncoder):
    pcm: bytes = b""
    sample_rate: int = 0
    targets: list[Path] = field(default_factory=list)

    def __post_init__(self) -> None:
        super().__init__()

    async def encode(
        self,
        pcm: AsyncIterator[bytes],
        *,
        sample_rate: int,
        target: Path,
        formats: Sequence[AudioFormat],
    ) -> list[AudioFile]:
        self.pcm = b"".join([chunk async for chunk in pcm])
        self.sample_rate = sample_rate
        self.targets.append(target)
        duration = len(self.pcm) / 2 / sample_rate
        return [AudioFile(target.with_suffix(f.extension), f, duration, 0) for f in formats]


def service(
    engine: FakeEngine | None = None, encoder: FFmpegEncoder | None = None, **settings: object
) -> TTSService:
    return TTSService(
        engine or FakeEngine(),
        encoder or RecordingEncoder(),
        TTSSettings.model_validate({"sentence_pause": 0.5, "paragraph_pause": 1.0, **settings}),
    )


async def test_synthesize_speaks_normalised_pieces_with_pauses(tmp_path: Path) -> None:
    engine, encoder = FakeEngine(), RecordingEncoder()
    tts = service(engine, encoder)

    files = await tts.synthesize(
        "Du hast 3 Mails. Ende.\n\nTschüss", lang="de", target=tmp_path / "d"
    )

    assert engine.calls == [
        ("Du hast drei Mails.", DE, "de"),
        ("Ende.", DE, "de"),
        ("Tschüss.", DE, "de"),
    ]
    assert engine.ensured == [DE]
    assert encoder.sample_rate == SAMPLE_RATE
    # Speech (one sample per char) plus 0.5 s after a sentence, 1 s after a paragraph.
    speech = sum(len(text) for text, _, _ in engine.calls)
    assert len(encoder.pcm) == 2 * (speech + SAMPLE_RATE // 2 + SAMPLE_RATE)
    assert [f.format for f in files] == [AudioFormat.OPUS]


async def test_user_voice_is_used_when_it_matches_the_language(tmp_path: Path) -> None:
    engine = FakeEngine()
    tts = service(engine)

    await tts.synthesize("Hallo.", lang="de", voice="de_DE-kerstin-low", target=tmp_path / "a")
    await tts.synthesize("Hello.", lang="en", voice="de_DE-kerstin-low", target=tmp_path / "b")
    await tts.synthesize("Hello.", lang="en", voice="../../etc/passwd", target=tmp_path / "c")

    assert [voice for _, voice, _ in engine.calls] == ["de_DE-kerstin-low", EN, EN]


def test_resolve_voice_uses_configured_defaults() -> None:
    tts = service(voice_de="de_DE-kerstin-low", voice_en="en_GB-alan-medium")

    assert tts.resolve_voice("de") == "de_DE-kerstin-low"
    assert tts.resolve_voice("en", None) == "en_GB-alan-medium"
    assert tts.resolve_voice("en", "en_US-amy-medium") == "en_US-amy-medium"


def test_voices_lists_installed_and_default_voices() -> None:
    engine = FakeEngine(installed={"de_DE-kerstin-low", DE, "fr_FR-siwis-medium"})

    voices = service(engine).voices()

    assert voices == [
        VoiceInfo("de_DE-kerstin-low", "fake", "de", installed=True),
        VoiceInfo(DE, "fake", "de", installed=True),
        VoiceInfo(EN, "fake", "en", installed=False),
    ]
    assert [v.id for v in service(engine).voices("en")] == [EN]


async def test_ensure_default_voices() -> None:
    engine = FakeEngine()

    await service(engine).ensure_default_voices()

    assert engine.ensured == [DE, EN]


async def test_missing_voice_fails_before_synthesis(tmp_path: Path) -> None:
    engine = FakeEngine(downloadable=False)

    with pytest.raises(VoiceNotAvailableError):
        await service(engine).synthesize("Hallo.", lang="de", target=tmp_path / "x")

    assert engine.calls == []


async def test_empty_text_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="nothing to speak"):
        await service().synthesize(" \n😀 ", lang="de", target=tmp_path / "x")


async def test_text_is_never_logged(tmp_path: Path) -> None:
    stream = io.StringIO()
    configure_logging(LoggingSettings(level="DEBUG", format="json"), stream=stream)

    await service().synthesize(SECRET, lang="de", target=tmp_path / "x")

    assert "Anna" not in stream.getvalue()
    logs = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [entry["event"] for entry in logs] == ["tts_synthesized"]
    assert logs[0]["voice"] == DE
    assert logs[0]["segments"] == 1


def test_create_tts_uses_settings(tmp_path: Path) -> None:
    settings = Settings(
        storage=StorageSettings(data_dir=tmp_path),
        tts=TTSSettings(ffmpeg_path="/opt/ffmpeg", opus_bitrate_kbps=48, mp3_bitrate_kbps=96),
    )

    tts = create_tts(settings)

    assert tts.engine.name == "piper"
    assert tts.encoder.ffmpeg == "/opt/ffmpeg"
    assert tts.encoder.opus_bitrate_kbps == 48
    assert tts.encoder.mp3_bitrate_kbps == 96


@requires_ffmpeg
async def test_synthesize_with_ffmpeg(tmp_path: Path) -> None:
    tts = service(encoder=FFmpegEncoder())

    files = await tts.synthesize(
        "Erster Satz. Zweiter Satz.",
        lang="de",
        target=tmp_path / "digest",
        formats=[AudioFormat.OPUS, AudioFormat.MP3],
    )

    assert [f.path.name for f in files] == ["digest.opus", "digest.mp3"]
    assert all(f.size_bytes > 0 and f.path.stat().st_size == f.size_bytes for f in files)
    assert files[0].duration == pytest.approx(0.5 + 23 / SAMPLE_RATE, abs=0.01)


async def test_encoder_failure_leaves_no_files(tmp_path: Path) -> None:
    tts = service(encoder=FFmpegEncoder(str(tmp_path / "missing-ffmpeg")))

    with pytest.raises(EncodingError):
        await tts.synthesize("Hallo.", lang="de", target=tmp_path / "x")

    assert os.listdir(tmp_path) == []
