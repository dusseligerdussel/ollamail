"""Child process of ``app.search.extract``: attachment bytes on stdin, plain text on stdout.

Usage: ``python -m app.search._extract_child <kind> <max_chars> <memory_mb> <cpu_seconds>``

Parsers of untrusted files (PDF in particular) can be slow, memory-hungry or buggy, so
they run here: a fresh process with an empty environment (no secrets), address-space,
CPU-time and file-size limits, killed by the parent on timeout. Only the standard library,
``pypdf`` and the HTML converter of the mail module are imported. Output and errors never
reach the logs (stderr is discarded by the parent).

Exit codes: 0 text written (possibly empty), 2 unreadable file, 3 encrypted file.
"""

import io
import re
import resource
import sys
import zipfile
from xml.etree import ElementTree

EXIT_UNREADABLE = 2
EXIT_ENCRYPTED = 3

# A DOCX's main XML part larger than this (uncompressed) is treated as unreadable.
DOCX_MAX_XML_BYTES = 64 * 1024 * 1024
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class Encrypted(Exception):
    pass


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


def extract_pdf(data: bytes, max_chars: int) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted and not reader.decrypt(""):
        raise Encrypted
    parts: list[str] = []
    total = 0
    for page in reader.pages:
        text = page.extract_text() or ""
        parts.append(text)
        total += len(text)
        if total >= max_chars:
            break
    return "\n\n".join(parts)


def extract_docx(data: bytes, max_chars: int) -> str:
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


def extract_html(data: bytes, max_chars: int) -> str:
    from app.mail.sanitize import html_to_text

    # Markup is several times longer than its text; cap what is parsed.
    return html_to_text(decode_text(data[: max_chars * 8]))


def extract_plain(data: bytes, max_chars: int) -> str:
    return decode_text(data[: max_chars * 4])


EXTRACTORS = {
    "pdf": extract_pdf,
    "docx": extract_docx,
    "html": extract_html,
    "text": extract_plain,
}


def main(argv: list[str]) -> int:
    kind, max_chars, memory_mb, cpu_seconds = argv[0], int(argv[1]), int(argv[2]), int(argv[3])
    limit_resources(memory_mb, cpu_seconds)
    data = sys.stdin.buffer.read()
    try:
        text = EXTRACTORS[kind](data, max_chars)
    except Encrypted:
        return EXIT_ENCRYPTED
    except Exception:
        return EXIT_UNREADABLE
    # NUL and other control characters cannot be stored in PostgreSQL text / are noise.
    text = _CONTROL.sub(" ", text)[:max_chars]
    sys.stdout.buffer.write(text.encode("utf-8", errors="replace"))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
