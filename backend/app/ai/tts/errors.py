"""TTS errors. Messages never contain the text being spoken."""


class TTSError(Exception):
    """Base class for TTS failures."""


class UnknownEngineError(TTSError):
    """No engine is registered under the configured name."""


class VoiceNotAvailableError(TTSError):
    """The voice is not installed and cannot be downloaded."""


class VoiceDownloadError(TTSError):
    """Downloading a voice failed (network, size limit, invalid files)."""


class SynthesisError(TTSError):
    """The engine failed to turn text into audio."""


class EncodingError(TTSError):
    """ffmpeg is missing or failed to encode the audio."""
