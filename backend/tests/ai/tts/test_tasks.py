import pytest

from app.ai.tts import tasks
from app.ai.tts.errors import VoiceDownloadError
from app.ai.tts.service import TTSService
from app.core.config import TTSSettings
from app.worker import TASK_MODULES, app
from tests.ai.tts.fakes import FakeEngine
from tests.ai.tts.test_service import RecordingEncoder


def test_task_is_registered_on_the_tts_queue() -> None:
    assert "app.ai.tts.tasks" in TASK_MODULES
    task = app.tasks["tts.ensure_voices"]
    assert task.queue == "tts"
    assert any(
        p.periodic_id == "tts_ensure_voices" for p in app.periodic_registry.periodic_tasks.values()
    )


def _use(monkeypatch: pytest.MonkeyPatch, engine: FakeEngine, **settings: object) -> None:
    service = TTSService(engine, RecordingEncoder(), TTSSettings.model_validate(settings))
    monkeypatch.setattr(tasks, "get_tts", lambda: service)


async def test_ensure_voices_downloads_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = FakeEngine()
    _use(monkeypatch, engine)

    await tasks.ensure_voices(timestamp=0)

    assert engine.ensured == ["de_DE-thorsten-medium", "en_US-ljspeech-medium"]


async def test_ensure_voices_is_a_noop_without_downloads(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = FakeEngine()
    _use(monkeypatch, engine, download_voices=False)

    await tasks.ensure_voices(timestamp=0)

    assert engine.ensured == []


async def test_ensure_voices_fails_for_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = FakeEngine()

    async def fail(voice: str) -> None:
        raise VoiceDownloadError("offline")

    engine.ensure_voice = fail  # type: ignore[method-assign]
    _use(monkeypatch, engine)

    with pytest.raises(VoiceDownloadError):
        await tasks.ensure_voices(timestamp=0)
