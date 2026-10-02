"""Text of attachments (PDF, DOCX, TXT, HTML, scanned PDFs and images) for the search index.

Every file is parsed in a separate Python process (``app.search._extract_child``) with
an empty environment and limits on memory, CPU time and run time, so a malicious or
broken attachment can neither stall the worker nor read its secrets. Files above the
size limit are skipped. Results carry a status code for logs and metrics; the text
itself is never logged.

OCR (Tesseract) runs in the same child process, never during indexing: ``Ocr.DETECT``
only reports that a PDF has scanned pages (``ocr_pending``), the job
``search.ocr_attachment`` on the ``ocr`` queue then extracts again with ``Ocr.RUN``.
"""

import asyncio
import contextlib
import enum
import math
import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app.core.config import SearchSettings

_BACKEND_DIR = Path(__file__).resolve().parents[2]
_CHILD_MODULE = "app.search._extract_child"
_EXIT_STATUS = {2: "unreadable", 3: "encrypted", 6: "ocr_failed", 7: "ocr_unavailable"}
_EXIT_NEEDS_OCR = 4
_EXIT_OCR = 5


class Kind(enum.StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    HTML = "html"
    TEXT = "text"
    IMAGE = "image"


class Ocr(enum.StrEnum):
    OFF = "off"
    # Text layers only; report scanned PDF pages (status ``ocr_pending``).
    DETECT = "detect"
    # Text layers plus Tesseract for scanned PDF pages and images.
    RUN = "run"


_CONTENT_TYPES: dict[str, Kind] = {
    "application/pdf": Kind.PDF,
    "application/x-pdf": Kind.PDF,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": Kind.DOCX,
    "text/html": Kind.HTML,
    "application/xhtml+xml": Kind.HTML,
    "text/plain": Kind.TEXT,
    "text/markdown": Kind.TEXT,
    "text/csv": Kind.TEXT,
    "image/png": Kind.IMAGE,
    "image/jpeg": Kind.IMAGE,
    "image/pjpeg": Kind.IMAGE,
    "image/tiff": Kind.IMAGE,
}
_EXTENSIONS: dict[str, Kind] = {
    ".pdf": Kind.PDF,
    ".docx": Kind.DOCX,
    ".html": Kind.HTML,
    ".htm": Kind.HTML,
    ".txt": Kind.TEXT,
    ".md": Kind.TEXT,
    ".csv": Kind.TEXT,
    ".png": Kind.IMAGE,
    ".jpg": Kind.IMAGE,
    ".jpeg": Kind.IMAGE,
    ".tif": Kind.IMAGE,
    ".tiff": Kind.IMAGE,
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
    ``encrypted``, ``failed``, ``ocr_pending`` (scanned pages, OCR not run yet),
    ``ocr_failed`` or ``ocr_unavailable`` (Tesseract missing). ``text`` is set for ``ok``
    and, if the PDF also has a text layer, for ``ocr_pending``. ``ocr``: the text includes
    recognised pages."""

    status: str
    text: str | None = None
    ocr: bool = False


def _child_env() -> dict[str, str]:
    # Nothing from the worker's environment (database URL, master key, API keys).
    return {
        "PATH": os.defpath,
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        # One core per Tesseract run (builds with OpenMP); parallelism is the ``ocr`` queue.
        "OMP_THREAD_LIMIT": "1",
    }


def _kill(process: asyncio.subprocess.Process) -> None:
    # The child runs in its own process group, so Tesseract is killed along with it.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        process.kill()


async def extract_text(
    data: bytes, kind: Kind, settings: SearchSettings, ocr: Ocr = Ocr.OFF
) -> Extraction:
    """Plain text of ``data`` (at most ``attachment_max_chars``), parsed in a child process."""
    if len(data) > settings.attachment_max_bytes:
        return Extraction("too_large")
    if kind is Kind.IMAGE and ocr is not Ocr.RUN:
        return Extraction("ocr_pending" if ocr is Ocr.DETECT else "unsupported")
    timeout = settings.ocr_timeout if ocr is Ocr.RUN else settings.extraction_timeout
    ocr_args = []
    if ocr is not Ocr.OFF:
        ocr_args = [ocr.value, str(settings.ocr_max_pages), settings.ocr_languages]
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
        *ocr_args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=_BACKEND_DIR,
        env=_child_env(),
        start_new_session=True,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(data), timeout)
    except TimeoutError:
        _kill(process)
        await process.wait()
        return Extraction("timeout")
    finally:
        if process.returncode is None:
            _kill(process)
            await process.wait()
    code = process.returncode
    if code not in (0, _EXIT_NEEDS_OCR, _EXIT_OCR):
        return Extraction(_EXIT_STATUS.get(code or 0, "failed"))
    text = stdout.decode("utf-8", errors="replace").strip() or None
    if code == _EXIT_NEEDS_OCR:
        return Extraction("ocr_pending", text)
    if text is None:
        return Extraction("empty")
    return Extraction("ok", text, ocr=code == _EXIT_OCR)
