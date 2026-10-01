from pathlib import Path

import pytest

from app.ai.tts import registry
from app.ai.tts.errors import UnknownEngineError
from app.ai.tts.piper import PiperEngine
from app.ai.tts.registry import create_engine, engine_names, register_engine
from app.core.config import Settings, StorageSettings, TTSSettings
from tests.ai.tts.fakes import FakeEngine


def settings(tmp_path: Path, engine: str) -> Settings:
    return Settings(storage=StorageSettings(data_dir=tmp_path), tts=TTSSettings(engine=engine))


def test_piper_is_the_default(tmp_path: Path) -> None:
    engine = create_engine(settings(tmp_path, "piper"))

    assert isinstance(engine, PiperEngine)
    assert engine.store.root == tmp_path / "tts" / "voices" / "piper"
    assert "piper" in engine_names()


def test_unknown_engine(tmp_path: Path) -> None:
    with pytest.raises(UnknownEngineError):
        create_engine(settings(tmp_path, "nope"))


def test_register_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "_ENGINES", dict(registry._ENGINES))
    fake = FakeEngine(name="kokoro")

    register_engine("kokoro", lambda _: fake)

    assert create_engine(settings(tmp_path, "kokoro")) is fake
    assert "kokoro" in engine_names()
