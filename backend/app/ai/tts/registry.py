"""Engine registry: ``OLLAMAIL_TTS_ENGINE`` selects an engine by name.

Built-in: ``piper``. Further engines (e.g. Kokoro or XTTS on GPU hosts) can be added
without touching features, either in code with :func:`register_engine` or as a plugin
package that declares an entry point in the group ``ollamail.tts_engines``::

    [project.entry-points."ollamail.tts_engines"]
    kokoro = "ollamail_kokoro:create_engine"

The entry point is a factory ``(Settings) -> TTSEngine``.
"""

from collections.abc import Callable
from importlib.metadata import entry_points

from app.ai.tts.base import TTSEngine
from app.ai.tts.errors import UnknownEngineError
from app.ai.tts.piper import PiperEngine
from app.core.config import Settings

EngineFactory = Callable[[Settings], TTSEngine]
ENTRY_POINT_GROUP = "ollamail.tts_engines"

_ENGINES: dict[str, EngineFactory] = {"piper": PiperEngine.from_settings}


def register_engine(name: str, factory: EngineFactory) -> None:
    _ENGINES[name] = factory


def _plugin_factory(name: str) -> EngineFactory | None:
    for entry_point in entry_points(group=ENTRY_POINT_GROUP, name=name):
        factory: EngineFactory = entry_point.load()
        return factory
    return None


def engine_names() -> list[str]:
    plugins = {entry_point.name for entry_point in entry_points(group=ENTRY_POINT_GROUP)}
    return sorted(set(_ENGINES) | plugins)


def create_engine(settings: Settings) -> TTSEngine:
    name = settings.tts.engine
    factory = _ENGINES.get(name) or _plugin_factory(name)
    if factory is None:
        raise UnknownEngineError(f"unknown TTS engine {name!r}")
    return factory(settings)
