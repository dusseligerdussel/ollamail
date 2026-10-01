"""Text-to-speech abstraction (docs/ARCHITECTURE.md §3.3).

Not imported eagerly by the API: the Piper engine pulls in ONNX Runtime only when a
voice is loaded.
"""

from app.ai.tts.base import TTSEngine
from app.ai.tts.errors import (
    EncodingError,
    SynthesisError,
    TTSError,
    UnknownEngineError,
    VoiceDownloadError,
    VoiceNotAvailableError,
)
from app.ai.tts.registry import create_engine, register_engine
from app.ai.tts.service import TTS_QUEUE, TTSService, create_tts, get_tts
from app.ai.tts.types import AudioFile, AudioFormat, Language, PCMAudio, VoiceInfo

__all__ = [
    "TTS_QUEUE",
    "AudioFile",
    "AudioFormat",
    "EncodingError",
    "Language",
    "PCMAudio",
    "SynthesisError",
    "TTSEngine",
    "TTSError",
    "TTSService",
    "UnknownEngineError",
    "VoiceDownloadError",
    "VoiceInfo",
    "VoiceNotAvailableError",
    "create_engine",
    "create_tts",
    "get_tts",
    "register_engine",
]
