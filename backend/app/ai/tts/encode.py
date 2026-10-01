"""Encoding PCM to Opus and MP3 with ffmpeg.

PCM is streamed into one ffmpeg process that writes all requested formats at once, so
long texts never have to be held in memory as a whole. Files are written under a
temporary name and renamed when ffmpeg succeeded; a failed run leaves nothing behind.
"""

import asyncio
import contextlib
import os
import shutil
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path

from app.ai.tts.errors import EncodingError
from app.ai.tts.types import AudioFile, AudioFormat

PART_SUFFIX = ".part"
# ffmpeg's stderr is kept for the exception only (never logged), bounded in size.
MAX_STDERR = 4000


def output_paths(target: Path, formats: Sequence[AudioFormat]) -> dict[AudioFormat, Path]:
    """``<target>.opus``, ``<target>.mp3``, ... for a target path without extension."""
    return {fmt: target.with_name(target.name + fmt.extension) for fmt in formats}


class FFmpegEncoder:
    def __init__(
        self,
        ffmpeg: str = "ffmpeg",
        *,
        opus_bitrate_kbps: int = 32,
        mp3_bitrate_kbps: int = 64,
    ) -> None:
        self.ffmpeg = ffmpeg
        self.opus_bitrate_kbps = opus_bitrate_kbps
        self.mp3_bitrate_kbps = mp3_bitrate_kbps

    def available(self) -> bool:
        return shutil.which(self.ffmpeg) is not None

    def _output_args(self, fmt: AudioFormat, path: Path) -> list[str]:
        if fmt is AudioFormat.OPUS:
            codec = ["-c:a", "libopus", "-b:a", f"{self.opus_bitrate_kbps}k", "-vbr", "on"]
            codec += ["-application", "voip", "-f", "ogg"]
        else:
            codec = ["-c:a", "libmp3lame", "-b:a", f"{self.mp3_bitrate_kbps}k", "-f", "mp3"]
        return [*codec, str(path)]

    def command(self, sample_rate: int, outputs: Mapping[AudioFormat, Path]) -> list[str]:
        args = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
        args += ["-f", "s16le", "-ar", str(sample_rate), "-ac", "1", "-i", "pipe:0"]
        # No metadata from the input, nothing that could identify the content.
        args += ["-map_metadata", "-1"]
        for fmt, path in outputs.items():
            args += self._output_args(fmt, path)
        return args

    async def encode(
        self,
        pcm: AsyncIterator[bytes],
        *,
        sample_rate: int,
        target: Path,
        formats: Sequence[AudioFormat],
    ) -> list[AudioFile]:
        """Encode the PCM stream into ``<target>.<ext>`` for each format."""
        if not formats:
            raise ValueError("at least one audio format is required")
        final = output_paths(target, formats)
        parts = {fmt: path.with_name(path.name + PART_SUFFIX) for fmt, path in final.items()}
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            process = await asyncio.create_subprocess_exec(
                *self.command(sample_rate, parts),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise EncodingError("ffmpeg could not be started") from exc

        assert process.stdin is not None and process.stderr is not None
        stderr_task = asyncio.create_task(process.stderr.read())
        total = 0
        try:
            try:
                async for chunk in pcm:
                    total += len(chunk)
                    process.stdin.write(chunk)
                    await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass  # ffmpeg exited early; the return code tells why.
            finally:
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    process.stdin.close()
                    await process.stdin.wait_closed()
            returncode = await process.wait()
            stderr = (await stderr_task)[-MAX_STDERR:].decode(errors="replace")
            if returncode != 0:
                raise EncodingError(f"ffmpeg failed with exit code {returncode}: {stderr}")
            if total == 0:
                raise EncodingError("no audio to encode")
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            stderr_task.cancel()
            for part in parts.values():
                part.unlink(missing_ok=True)
            raise

        duration = total / 2 / sample_rate
        files = []
        for fmt, part in parts.items():
            os.replace(part, final[fmt])
            files.append(AudioFile(final[fmt], fmt, duration, final[fmt].stat().st_size))
        return files
