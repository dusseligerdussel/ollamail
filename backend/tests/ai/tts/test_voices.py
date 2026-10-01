import asyncio
from pathlib import Path

import httpx
import pytest

from app.ai.tts import voices
from app.ai.tts.errors import VoiceDownloadError, VoiceNotAvailableError
from app.ai.tts.types import VoiceInfo
from app.ai.tts.voices import (
    DEFAULT_VOICE_LICENSES,
    PiperVoiceStore,
    voice_language,
    voice_url_path,
)
from app.core.config import TTSSettings

BASE = "https://voices.test/piper"
VOICE = "de_DE-thorsten-medium"
MODEL = b"onnx-model-bytes" * 100
CONFIG = b'{"audio": {"sample_rate": 22050}}'


def store(
    tmp_path: Path, handler: httpx.MockTransport | None = None, **kwargs: object
) -> PiperVoiceStore:
    return PiperVoiceStore(tmp_path, base_url=BASE + "/", transport=handler, **kwargs)  # type: ignore[arg-type]


def serve(files: dict[str, bytes], requests: list[str] | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(str(request.url))
        body = files.get(str(request.url))
        return httpx.Response(200, content=body) if body is not None else httpx.Response(404)

    return httpx.MockTransport(handler)


def voice_files(
    voice: str = VOICE, model: bytes = MODEL, config: bytes = CONFIG
) -> dict[str, bytes]:
    url = f"{BASE}/{voice_url_path(voice)}"
    return {f"{url}.onnx": model, f"{url}.onnx.json": config}


@pytest.mark.parametrize(
    ("voice", "lang"),
    [
        ("de_DE-thorsten-medium", "de"),
        ("de_AT-hmm-x_low", "de"),
        ("en_GB-alan-low", "en"),
        ("en_US-libritts_r-high", "en"),
        ("fr_FR-siwis-medium", None),
        ("../de_DE-thorsten-medium", None),
        ("de_DE-thorsten-medium/../../x", None),
        ("de_DE-thorsten", None),
        ("", None),
    ],
)
def test_voice_language(voice: str, lang: str | None) -> None:
    assert voice_language(voice) == lang


def test_voice_url_path() -> None:
    assert voice_url_path(VOICE) == "de/de_DE/thorsten/medium/de_DE-thorsten-medium"
    with pytest.raises(VoiceNotAvailableError):
        voice_url_path("../../etc/passwd")


def test_default_voices_have_known_licenses() -> None:
    settings = TTSSettings()

    assert set(DEFAULT_VOICE_LICENSES) == {settings.voice_de, settings.voice_en}


async def test_download_installs_voice(tmp_path: Path) -> None:
    requests: list[str] = []
    voice_store = store(tmp_path, serve(voice_files(), requests))

    await voice_store.ensure(VOICE)
    await voice_store.ensure(VOICE)  # installed: no second download

    root = tmp_path / "tts" / "voices" / "piper"
    assert (root / f"{VOICE}.onnx").read_bytes() == MODEL
    assert (root / f"{VOICE}.onnx.json").read_bytes() == CONFIG
    assert len(requests) == 2
    assert voice_store.installed() == [VoiceInfo(VOICE, "piper", "de", installed=True)]
    assert [p.name for p in root.iterdir() if p.name.startswith(".tmp")] == []


async def test_concurrent_ensure_downloads_once(tmp_path: Path) -> None:
    requests: list[str] = []
    voice_store = store(tmp_path, serve(voice_files(), requests))

    await asyncio.gather(*(voice_store.ensure(VOICE) for _ in range(3)))

    assert len(requests) == 2


async def test_unknown_voice(tmp_path: Path) -> None:
    voice_store = store(tmp_path, serve({}))

    with pytest.raises(VoiceNotAvailableError):
        await voice_store.ensure("de_DE-nobody-medium")

    assert not voice_store.is_installed("de_DE-nobody-medium")


async def test_invalid_voice_id_is_rejected_without_request(tmp_path: Path) -> None:
    requests: list[str] = []
    voice_store = store(tmp_path, serve({}, requests))

    with pytest.raises(VoiceNotAvailableError):
        await voice_store.ensure("../../secret")

    assert requests == []
    assert not voice_store.is_installed("../../secret")


async def test_downloads_disabled(tmp_path: Path) -> None:
    requests: list[str] = []
    voice_store = store(tmp_path, serve(voice_files(), requests), download=False)

    with pytest.raises(VoiceNotAvailableError, match="downloads are off"):
        await voice_store.ensure(VOICE)

    assert requests == []


async def test_manually_copied_voice_is_used_without_download(tmp_path: Path) -> None:
    voice_store = store(tmp_path, serve({}), download=False)
    voice_store.root.mkdir(parents=True)
    voice_store.model_path(VOICE).write_bytes(MODEL)
    voice_store.config_path(VOICE).write_bytes(CONFIG)
    # Incomplete or foreign files are not listed.
    (voice_store.root / "en_US-ljspeech-medium.onnx").write_bytes(MODEL)
    (voice_store.root / "notes.txt").write_text("x")

    await voice_store.ensure(VOICE)

    assert [v.id for v in voice_store.installed()] == [VOICE]


async def test_invalid_config_is_rejected(tmp_path: Path) -> None:
    voice_store = store(tmp_path, serve(voice_files(config=b"<html>")))

    with pytest.raises(VoiceDownloadError, match="JSON"):
        await voice_store.ensure(VOICE)

    assert list(voice_store.root.iterdir()) == []


async def test_size_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(voices, "MAX_MODEL_BYTES", 100)
    voice_store = store(tmp_path, serve(voice_files()))

    with pytest.raises(VoiceDownloadError, match="size limit"):
        await voice_store.ensure(VOICE)

    assert not voice_store.model_path(VOICE).exists()
    assert [p.name for p in voice_store.root.iterdir() if p.name.startswith(".tmp")] == []


async def test_network_error(tmp_path: Path) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    voice_store = store(tmp_path, httpx.MockTransport(fail))

    with pytest.raises(VoiceDownloadError):
        await voice_store.ensure(VOICE)

    assert not voice_store.is_installed(VOICE)


async def test_server_error(tmp_path: Path) -> None:
    voice_store = store(tmp_path, httpx.MockTransport(lambda request: httpx.Response(503)))

    with pytest.raises(VoiceDownloadError):
        await voice_store.ensure(VOICE)
