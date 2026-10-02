"""Text of attachments (PDF, DOCX, TXT, HTML) for the search index. No OCR.

Every file is parsed in a separate Python process (``app.search._extract_child``) with
an empty environment and limits on memory, CPU time and run time, so a malicious or
broken attachment can neither stall the worker nor read its secrets. Files above the
size limit are skipped. Results carry a status code for logs and metrics; the text
itself is never logged.
"""

import asyncio
import contextlib
import enum
import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app.core.config import SearchSettings

_BACKEND_DIR = Path(__file__).resolve().parents[2]
_CHILD_MODULE = "app.search._extract_child"
_EXIT_STATUS = {2: "unreadable", 3: "encrypted"}


class Kind(enum.StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    HTML = "html"
    TEXT = "text"


_CONTENT_TYPES: dict[str, Kind] = {
    "application/pdf": Kind.PDF,
    "application/x-pdf": Kind.PDF,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": Kind.DOCX,
    "text/html": Kind.HTML,
    "application/xhtml+xml": Kind.HTML,
    "text/plain": Kind.TEXT,
    "text/markdown": Kind.TEXT,
    "text/csv": Kind.TEXT,
}
_EXTENSIONS: dict[str, Kind] = {
    ".pdf": Kind.PDF,
    ".docx": Kind.DOCX,
    ".html": Kind.HTML,
    ".htm": Kind.HTML,
    ".txt": Kind.TEXT,
    ".md": Kind.TEXT,
    ".csv": Kind.TEXT,
}
# Generic types under which mail clients send any file; the extension decides then.
_GENERIC = frozenset({"application/octet-stream", "application/x-download", "binary/octet-stream"})


def detect_kind(content_type: str, filename: str | None) -> Kind | None:
    """Supported kind of an attachment, or ``None``."""
    mime = content_type.split(";", 1)[0].strip().lower()
    if mime in _CONTENT_TYPES:
        return _CONTENT_TYPES[mime]
    if mime in _GENERIC and filename:
        return _EXTENSIONS.get(PurePosixPath(filename.lower()).suffix)
    return None


@dataclass(frozen=True)
class Extraction:
    """``status``: ``ok``, ``empty``, ``too_large``, ``timeout``, ``unreadable``,
    ``encrypted`` or ``failed``. ``text`` is set for ``ok`` only."""

    status: str
    text: str | None = None


def _child_env() -> dict[str, str]:
    # Nothing from the worker's environment (database URL, master key, API keys).
    return {
        "PATH": os.defpath,
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
    }


async def extract_text(data: bytes, kind: Kind, settings: SearchSettings) -> Extraction:
    """Plain text of ``data`` (at most ``attachment_max_chars``), parsed in a child process."""
    if len(data) > settings.attachment_max_bytes:
        return Extraction("too_large")
    timeout = settings.extraction_timeout
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-E",
        "-s",
        "-m",
        _CHILD_MODULE,
        kind.value,
        str(settings.attachment_max_chars),
        str(settings.extraction_max_memory_mb),
        str(math.ceil(timeout)),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=_BACKEND_DIR,
        env=_child_env(),
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(data), timeout)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        return Extraction("timeout")
    if process.returncode != 0:
        return Extraction(_EXIT_STATUS.get(process.returncode or 0, "failed"))
    text = stdout.decode("utf-8", errors="replace").strip()
    return Extraction("ok", text) if text else Extraction("empty")
