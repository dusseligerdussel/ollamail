"""Piper engine. The tests at the end need ``piper-tts`` and a voice; they are skipped
otherwise. Point them at a directory with ``<voice>.onnx`` and ``<voice>.onnx.json``::

    OLLAMAIL_TEST_PIPER_VOICES=/path/to/voices uv run pytest -m piper
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app.ai.tts.encode import FFmpegEncoder
from app.ai.tts.errors import VoiceNotAvailableError
from app.ai.tts.piper import PiperEngine, piper_available
from app.ai.tts.service import TTSService
from app.ai.tts.types import AudioFormat
from app.ai.tts.voices import PiperVoiceStore, voice_language
from app.core.config import TTSSettings

VOICES_DIR = os.environ.get("OLLAMAIL_TEST_PIPER_VOICES")


def engine(data_dir: Path) -> PiperEngine:
    store = PiperVoiceStore(data_dir, base_url="https://voices.test", download=False)
    return PiperEngine(store)


async def test_voice_must_speak_the_language(tmp_path: Path) -> None:
    with pytest.raises(VoiceNotAvailableError, match="language"):
        await engine(tmp_path).synthesize("Hello.", "de_DE-thorsten-medium", "en")


async def test_voice_must_be_installed(tmp_path: Path) -> None:
    with pytest.raises(VoiceNotAvailableError, match="not installed"):
        await engine(tmp_path).synthesize("Hallo.", "de_DE-thorsten-medium", "de")


def test_voice_ids(tmp_path: Path) -> None:
    piper = engine(tmp_path)

    assert piper.is_valid_voice("en_GB-alan-low")
    assert not piper.is_valid_voice("alan")
    assert piper.voice_language("de_DE-thorsten-medium") == "de"
    assert piper.installed_voices() == []


def _test_voice() -> tuple[Path, str]:
    if not piper_available():
        pytest.skip("piper-tts not installed")
    if not VOICES_DIR:
        pytest.skip("OLLAMAIL_TEST_PIPER_VOICES not set")
    for model in sorted(Path(VOICES_DIR).glob("*.onnx")):
        voice = model.name.removesuffix(".onnx")
        if voice_language(voice) and model.with_suffix(".onnx.json").is_file():
            return model.parent, voice
    pytest.skip(f"no German or English Piper voice in {VOICES_DIR}")


@pytest.mark.piper
async def test_real_piper_synthesis(tmp_path: Path) -> None:
    source, voice = _test_voice()
    voices_dir = tmp_path / "tts" / "voices" / "piper"
    voices_dir.mkdir(parents=True)
    for suffix in (".onnx", ".onnx.json"):
        (voices_dir / f"{voice}{suffix}").symlink_to(source / f"{voice}{suffix}")
    lang = voice_language(voice)
    assert lang is not None
    piper = engine(tmp_path)

    audio = await piper.synthesize("Hello. This is a test.", voice, lang)

    assert audio.sample_rate in {16000, 22050}
    assert 0.5 < audio.duration < 10

    if shutil.which("ffmpeg") is None:
        return
    tts = TTSService(piper, FFmpegEncoder(), TTSSettings(voice_de=voice, voice_en=voice))
    files = await tts.synthesize(
        "Am 3.10.2026 um 14:30 Uhr. Second sentence.",
        lang=lang,
        target=tmp_path / "digest",
        formats=[AudioFormat.OPUS, AudioFormat.MP3],
    )
    assert all(f.size_bytes > 1000 for f in files)
    await tts.aclose()


def test_onnxruntime_telemetry_is_disabled(tmp_path: Path) -> None:
    """ONNX Runtime must not create a device ID or queue telemetry events."""
    pytest.importorskip("onnxruntime")
    script = (
        "import app.ai.tts.piper\n"
        "import onnxruntime as ort\n"
        "from onnxruntime.datasets import get_example\n"
        "ort.InferenceSession(get_example('mul_1.onnx'), providers=['CPUExecutionProvider'])\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "ORT_DISABLE_TELEMETRY"}
    env |= {"HOME": str(tmp_path), "XDG_CACHE_HOME": str(tmp_path / "cache")}

    subprocess.run(
        [sys.executable, "-c", script], check=True, env=env, cwd=Path(__file__).parents[3]
    )

    assert list(tmp_path.rglob("*")) == []
