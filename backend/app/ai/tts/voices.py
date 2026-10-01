"""Piper voices in the data volume: lookup, download and listing.

Layout: ``<data_dir>/tts/voices/piper/<voice>.onnx`` plus ``<voice>.onnx.json``. Voices
are never part of the image. Missing voices are downloaded from
``OLLAMAIL_TTS_VOICE_BASE_URL`` (the ``rhasspy/piper-voices`` repository by default);
without internet access, copy both files into the directory by hand.

Voice IDs follow Piper's naming ``<lang>_<REGION>-<name>-<quality>``, e.g.
``de_DE-thorsten-medium``; the language is part of the ID. The pattern doubles as path
validation, so a voice ID from user settings can never point outside the directory.
"""

import asyncio
import contextlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.ai.tts.errors import VoiceDownloadError, VoiceNotAvailableError
from app.ai.tts.types import LANGUAGES, Language, VoiceInfo
from app.core.logging import get_logger

log = get_logger(__name__)

VOICES_DIR = Path("tts") / "voices"
VOICE_PATTERN = re.compile(
    r"^(?P<family>[a-z]{2,3})_(?P<region>[A-Z]{2})-(?P<name>[a-z0-9_]+)-(?P<quality>x_low|low|medium|high)$"
)
# Largest Piper models are ~120 MB; anything far beyond is not a voice.
MAX_MODEL_BYTES = 300 * 1024 * 1024
MAX_CONFIG_BYTES = 1024 * 1024


@dataclass(frozen=True)
class VoiceLicense:
    dataset: str
    license: str
    url: str


# Licenses of the default voices (from the model cards in rhasspy/piper-voices). Other
# voices may carry restrictions (e.g. non-commercial datasets); check before configuring.
DEFAULT_VOICE_LICENSES: dict[str, VoiceLicense] = {
    "de_DE-thorsten-medium": VoiceLicense(
        "Thorsten-Voice (Thorsten Müller)", "CC0-1.0", "https://www.thorsten-voice.de"
    ),
    "en_US-ljspeech-medium": VoiceLicense(
        "LJ Speech", "Public Domain", "https://keithito.com/LJ-Speech-Dataset/"
    ),
}


def parse_voice(voice: str) -> re.Match[str] | None:
    return VOICE_PATTERN.match(voice)


def voice_language(voice: str) -> Language | None:
    match = parse_voice(voice)
    if match is None:
        return None
    family = match.group("family")
    return next((lang for lang in LANGUAGES if lang == family), None)


def voice_url_path(voice: str) -> str:
    """Path below the base URL: ``de/de_DE/thorsten/medium/de_DE-thorsten-medium``."""
    match = parse_voice(voice)
    if match is None:
        raise VoiceNotAvailableError("invalid voice ID")
    family, region, name, quality = match.group("family", "region", "name", "quality")
    return f"{family}/{family}_{region}/{name}/{quality}/{voice}"


class PiperVoiceStore:
    def __init__(
        self,
        data_dir: Path,
        *,
        base_url: str,
        download: bool = True,
        timeout: float = 600.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.root = data_dir / VOICES_DIR / "piper"
        self.base_url = base_url.rstrip("/")
        self.download_enabled = download
        self.timeout = timeout
        self._transport = transport
        self._locks: dict[str, asyncio.Lock] = {}

    def model_path(self, voice: str) -> Path:
        if parse_voice(voice) is None:
            raise VoiceNotAvailableError("invalid voice ID")
        return self.root / f"{voice}.onnx"

    def config_path(self, voice: str) -> Path:
        return self.model_path(voice).with_suffix(".onnx.json")

    def is_installed(self, voice: str) -> bool:
        if parse_voice(voice) is None:
            return False
        return self.model_path(voice).is_file() and self.config_path(voice).is_file()

    def installed(self) -> list[VoiceInfo]:
        if not self.root.is_dir():
            return []
        voices = []
        for model in sorted(self.root.glob("*.onnx")):
            voice = model.name.removesuffix(".onnx")
            lang = voice_language(voice)
            if lang is not None and self.is_installed(voice):
                voices.append(VoiceInfo(voice, "piper", lang, installed=True))
        return voices

    async def ensure(self, voice: str) -> None:
        """Download ``voice`` unless it is installed. Safe to call concurrently."""
        if self.is_installed(voice):
            return
        if parse_voice(voice) is None:
            raise VoiceNotAvailableError("invalid voice ID")
        if not self.download_enabled:
            raise VoiceNotAvailableError(f"voice {voice} is not installed, downloads are off")
        lock = self._locks.setdefault(voice, asyncio.Lock())
        async with lock:
            if self.is_installed(voice):
                return
            await self._download(voice)

    async def _download(self, voice: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        url = f"{self.base_url}/{voice_url_path(voice)}"
        log.info("tts_voice_download_started", voice=voice)
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, follow_redirects=True, transport=self._transport
            ) as client:
                # Config first: the model file appearing marks a complete installation.
                config = await self._fetch(client, f"{url}.onnx.json", self.config_path(voice))
                try:
                    json.loads(config.read_bytes())
                except ValueError as exc:
                    config.unlink(missing_ok=True)
                    raise VoiceDownloadError("voice config is not valid JSON") from exc
                await self._fetch(client, f"{url}.onnx", self.model_path(voice))
        except httpx.HTTPError as exc:
            log.warning("tts_voice_download_failed", voice=voice, error_type=type(exc).__name__)
            raise VoiceDownloadError(f"download of voice {voice} failed") from exc
        log.info("tts_voice_downloaded", voice=voice)

    async def _fetch(self, client: httpx.AsyncClient, url: str, path: Path) -> Path:
        limit = MAX_CONFIG_BYTES if path.name.endswith(".json") else MAX_MODEL_BYTES
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as handle:
                async with client.stream("GET", url) as response:
                    if response.status_code == 404:
                        raise VoiceNotAvailableError("voice does not exist at the download URL")
                    response.raise_for_status()
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > limit:
                            raise VoiceDownloadError("voice file exceeds the size limit")
                        handle.write(chunk)
            if size == 0:
                raise VoiceDownloadError("voice file is empty")
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise
        return path
