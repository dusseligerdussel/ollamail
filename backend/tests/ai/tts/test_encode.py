import json
import os
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.ai.tts.encode import FFmpegEncoder, output_paths
from app.ai.tts.errors import EncodingError
from app.ai.tts.types import AudioFormat
from tests.ai.tts.conftest import requires_ffmpeg

RATE = 22050


async def tone(seconds: float, chunks: int = 4) -> AsyncIterator[bytes]:
    samples = int(RATE * seconds)
    for _ in range(chunks):
        yield b"\x00\x10" * (samples // chunks)


def test_output_paths() -> None:
    paths = output_paths(Path("/data/digests/u/d1"), [AudioFormat.OPUS, AudioFormat.MP3])

    assert paths == {
        AudioFormat.OPUS: Path("/data/digests/u/d1.opus"),
        AudioFormat.MP3: Path("/data/digests/u/d1.mp3"),
    }


def test_command_writes_all_formats_in_one_run() -> None:
    encoder = FFmpegEncoder("ffmpeg", opus_bitrate_kbps=24, mp3_bitrate_kbps=96)

    command = encoder.command(
        RATE, {AudioFormat.OPUS: Path("a.opus"), AudioFormat.MP3: Path("a.mp3")}
    )

    assert command[:3] == ["ffmpeg", "-hide_banner", "-loglevel"]
    assert " ".join(command).count("-i pipe:0") == 1
    assert "-ar 22050" in " ".join(command)
    assert "-c:a libopus -b:a 24k" in " ".join(command)
    assert "-c:a libmp3lame -b:a 96k" in " ".join(command)
    assert command[-1] == "a.mp3"


def test_media_types() -> None:
    assert AudioFormat.OPUS.media_type == "audio/ogg"
    assert AudioFormat.MP3.media_type == "audio/mpeg"


def _probe(path: Path) -> dict[str, str]:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
        check=True,
        capture_output=True,
    )
    stream: dict[str, str] = json.loads(result.stdout)["streams"][0]
    return stream


@requires_ffmpeg
async def test_encode_opus_and_mp3(tmp_path: Path) -> None:
    target = tmp_path / "out" / "digest"

    files = await FFmpegEncoder().encode(
        tone(2.0), sample_rate=RATE, target=target, formats=[AudioFormat.OPUS, AudioFormat.MP3]
    )

    assert [f.format for f in files] == [AudioFormat.OPUS, AudioFormat.MP3]
    assert files[0].duration == pytest.approx(2.0, abs=0.01)
    assert sorted(p.name for p in target.parent.iterdir()) == ["digest.mp3", "digest.opus"]
    assert _probe(files[0].path)["codec_name"] == "opus"
    assert _probe(files[1].path)["codec_name"] == "mp3"


@requires_ffmpeg
async def test_failed_encoding_removes_partial_files(tmp_path: Path) -> None:
    async def broken() -> AsyncIterator[bytes]:
        yield b"\x00\x00" * 1000
        raise RuntimeError("engine crashed")

    with pytest.raises(RuntimeError):
        await FFmpegEncoder().encode(
            broken(), sample_rate=RATE, target=tmp_path / "d", formats=[AudioFormat.OPUS]
        )

    assert os.listdir(tmp_path) == []


@requires_ffmpeg
async def test_empty_audio_is_an_error(tmp_path: Path) -> None:
    async def nothing() -> AsyncIterator[bytes]:
        return
        yield b""

    with pytest.raises(EncodingError):
        await FFmpegEncoder().encode(
            nothing(), sample_rate=RATE, target=tmp_path / "d", formats=[AudioFormat.MP3]
        )

    assert os.listdir(tmp_path) == []


@requires_ffmpeg
async def test_ffmpeg_error_is_reported(tmp_path: Path) -> None:
    encoder = FFmpegEncoder(opus_bitrate_kbps=32)
    # An impossible sample rate makes ffmpeg fail.
    with pytest.raises(EncodingError, match="exit code"):
        await encoder.encode(
            tone(0.1), sample_rate=0, target=tmp_path / "d", formats=[AudioFormat.OPUS]
        )

    assert os.listdir(tmp_path) == []


async def test_missing_ffmpeg(tmp_path: Path) -> None:
    encoder = FFmpegEncoder(str(tmp_path / "no-ffmpeg"))

    assert not encoder.available()
    with pytest.raises(EncodingError, match="could not be started"):
        await encoder.encode(
            tone(0.1), sample_rate=RATE, target=tmp_path / "d", formats=[AudioFormat.OPUS]
        )


async def test_formats_are_required(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        await FFmpegEncoder().encode(tone(0.1), sample_rate=RATE, target=tmp_path / "d", formats=[])
