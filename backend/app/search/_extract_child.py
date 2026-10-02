"""Child process of ``app.search.extract``: attachment bytes on stdin, plain text on stdout.

Usage: ``python -m app.search._extract_child <kind> <max_chars> <memory_mb> <cpu_seconds>
[<ocr> <ocr_max_pages> <ocr_languages>]``

Parsers of untrusted files (PDF in particular) can be slow, memory-hungry or buggy, so
they run here: a fresh process with an empty environment (no secrets), address-space,
CPU-time and file-size limits, killed by the parent on timeout. Only the standard library,
``pypdf``, ``pypdfium2`` (rendering pages for OCR) and the HTML converter of the mail
module are imported. Output and errors never reach the logs (stderr is discarded by the
parent).

OCR (``<ocr>``): ``off`` reads text layers only. ``detect`` reads text layers and exits
with ``EXIT_NEEDS_OCR`` if a PDF has pages with images but no text (the parent queues an
OCR job). ``run`` reads text layers and runs Tesseract on PDF pages without text (at most
``<ocr_max_pages>``) and on images. Tesseract is a child of this process: it inherits the
limits and the empty environment, gets the image on stdin and writes text to stdout.

Exit codes: 0 text written (possibly empty), 2 unreadable file, 3 encrypted file,
4 text layer written but pages need OCR, 5 text written including OCR, 6 OCR failed,
7 Tesseract is not installed.
"""

import io
import math
import re
import resource
import subprocess
import sys
import zipfile
from typing import Any
from xml.etree import ElementTree

EXIT_UNREADABLE = 2
EXIT_ENCRYPTED = 3
EXIT_NEEDS_OCR = 4
EXIT_OCR = 5
EXIT_OCR_FAILED = 6
EXIT_OCR_UNAVAILABLE = 7

# A PDF page with less text than this (and an image) counts as scanned.
MIN_PAGE_CHARS = 16
# Rendering resolution for OCR; pages larger than ``MAX_RENDER_PIXELS`` are scaled down.
OCR_DPI = 300
MAX_RENDER_PIXELS = 40_000_000

# A DOCX's main XML part larger than this (uncompressed) is treated as unreadable.
DOCX_MAX_XML_BYTES = 64 * 1024 * 1024
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class Encrypted(Exception):
    pass


class OcrFailed(Exception):
    pass


class OcrUnavailable(Exception):
    pass


class Ocr:
    """Tesseract with the settings of this run; counts what it did."""

    def __init__(self, mode: str, max_pages: int, languages: str) -> None:
        self.mode = mode
        self.max_pages = max_pages
        self.languages = languages
        # Pages (or images) recognised so far.
        self.pages = 0
        # ``detect``: a page needs OCR.
        self.needed = False

    def recognise(self, image: bytes, dpi: int | None = None) -> str:
        command = ["tesseract", "stdin", "stdout", "-l", self.languages]
        if dpi is not None:
            command += ["--dpi", str(dpi)]
        try:
            result = subprocess.run(command, input=image, capture_output=True, check=False)
        except FileNotFoundError:
            raise OcrUnavailable from None
        if result.returncode != 0:
            raise OcrFailed
        self.pages += 1
        return result.stdout.decode("utf-8", errors="replace")


def _has_image(resources: Any, depth: int = 0) -> bool:
    """Whether a page's resources draw an image (directly or inside a form, 3 levels)."""
    if depth > 3 or resources is None:
        return False
    resources = resources.get_object()
    xobjects = resources.get("/XObject")
    if xobjects is None:
        return False
    for ref in xobjects.get_object().values():
        xobject = ref.get_object()
        subtype = xobject.get("/Subtype")
        if subtype == "/Image":
            return True
        if subtype == "/Form" and _has_image(xobject.get("/Resources"), depth + 1):
            return True
    return False


def render_pgm(document: Any, index: int) -> bytes:
    """Page ``index`` of a ``pypdfium2.PdfDocument`` as greyscale PGM for Tesseract."""
    page = document[index]
    try:
        width, height = page.get_size()
        scale = OCR_DPI / 72
        pixels = width * height * scale * scale
        if pixels > MAX_RENDER_PIXELS:
            # Slightly below the limit: the page size in pixels is rounded up.
            scale *= math.sqrt(MAX_RENDER_PIXELS / pixels) * 0.999
        bitmap = page.render(scale=scale, grayscale=True)
        try:
            w, h, stride = bitmap.width, bitmap.height, bitmap.stride
            buffer = bytes(bitmap.buffer)
        finally:
            bitmap.close()
    finally:
        page.close()
    if stride != w:
        buffer = b"".join(buffer[y * stride : y * stride + w] for y in range(h))
    return b"P5\n%d %d\n255\n" % (w, h) + buffer


def limit_resources(memory_mb: int, cpu_seconds: int) -> None:
    memory = memory_mb * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    # No files are written and no core dumps (they would contain the attachment).
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def decode_text(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def extract_pdf(data: bytes, max_chars: int, ocr: Ocr | None = None) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted and not reader.decrypt(""):
        raise Encrypted
    document: Any = None
    parts: list[str] = []
    total = 0
    try:
        for index, page in enumerate(reader.pages):
            text = page.extract_text() or ""
            if (
                ocr is not None
                and ocr.mode != "off"
                and len(text.strip()) < MIN_PAGE_CHARS
                and _has_image(page.get("/Resources"))
            ):
                if ocr.mode == "detect":
                    ocr.needed = True
                elif ocr.pages < ocr.max_pages:
                    if document is None:
                        import pypdfium2

                        document = pypdfium2.PdfDocument(data)
                    text = ocr.recognise(render_pgm(document, index), dpi=OCR_DPI)
            parts.append(text)
            total += len(text)
            if total >= max_chars:
                break
    finally:
        if document is not None:
            document.close()
    return "\n\n".join(parts)


def extract_image(data: bytes, max_chars: int, ocr: Ocr | None = None) -> str:
    if ocr is None or ocr.mode != "run":
        return ""
    return ocr.recognise(data)


def extract_docx(data: bytes, max_chars: int, ocr: Ocr | None = None) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if "EncryptedPackage" in archive.namelist():
            raise Encrypted
        info = archive.getinfo("word/document.xml")
        if info.file_size > DOCX_MAX_XML_BYTES:
            raise ValueError("document part too large")
        xml = archive.read(info)
    # WordprocessingML never needs a DTD; refusing one rules out entity expansion attacks.
    if b"<!DOCTYPE" in xml[:4096].upper() or b"<!ENTITY" in xml.upper():
        raise ValueError("DTD in document part")
    paragraphs: list[str] = []
    current: list[str] = []
    total = 0
    for event, element in ElementTree.iterparse(io.BytesIO(xml), events=("start", "end")):
        tag = element.tag
        if event == "start":
            continue
        if tag == f"{_W}t" and element.text:
            current.append(element.text)
        elif tag == f"{_W}tab":
            current.append("\t")
        elif tag in (f"{_W}br", f"{_W}cr"):
            current.append("\n")
        elif tag == f"{_W}p":
            paragraph = "".join(current)
            paragraphs.append(paragraph)
            total += len(paragraph)
            current = []
            element.clear()
            if total >= max_chars:
                break
    return "\n\n".join(p for p in paragraphs if p.strip())


def extract_html(data: bytes, max_chars: int, ocr: Ocr | None = None) -> str:
    from app.mail.sanitize import html_to_text

    # Markup is several times longer than its text; cap what is parsed.
    return html_to_text(decode_text(data[: max_chars * 8]))


def extract_plain(data: bytes, max_chars: int, ocr: Ocr | None = None) -> str:
    return decode_text(data[: max_chars * 4])


EXTRACTORS = {
    "pdf": extract_pdf,
    "docx": extract_docx,
    "html": extract_html,
    "text": extract_plain,
    "image": extract_image,
}


def main(argv: list[str]) -> int:
    kind, max_chars, memory_mb, cpu_seconds = argv[0], int(argv[1]), int(argv[2]), int(argv[3])
    ocr = Ocr(argv[4], int(argv[5]), argv[6]) if len(argv) > 4 else None
    limit_resources(memory_mb, cpu_seconds)
    data = sys.stdin.buffer.read()
    try:
        text = EXTRACTORS[kind](data, max_chars, ocr)
    except Encrypted:
        return EXIT_ENCRYPTED
    except OcrUnavailable:
        return EXIT_OCR_UNAVAILABLE
    except OcrFailed:
        return EXIT_OCR_FAILED
    except Exception:
        return EXIT_UNREADABLE
    # NUL and other control characters cannot be stored in PostgreSQL text / are noise.
    text = _CONTROL.sub(" ", text)[:max_chars]
    sys.stdout.buffer.write(text.encode("utf-8", errors="replace"))
    sys.stdout.flush()
    if ocr is not None and ocr.needed:
        return EXIT_NEEDS_OCR
    if ocr is not None and ocr.pages:
        return EXIT_OCR
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
