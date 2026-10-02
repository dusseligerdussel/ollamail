"""Split mail text into overlapping chunks for the search index.

Each chunk carries a short header context (sender, date, subject, attachment name), so
a chunk found on its own still says where it comes from, for the embedding model as
well as for the full-text index. Chunk boundaries follow paragraphs, then lines, then
sentences, then words; neighbouring chunks share ``overlap`` characters so a sentence
cut at a boundary is still found in one piece.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

# Separators from coarse to fine; the first one that yields pieces of the target size wins.
_SEPARATORS = (
    re.compile(r"\n[ \t]*\n\s*"),
    re.compile(r"\n"),
    re.compile(r"(?<=[.!?…])\s+"),
    re.compile(r"\s+"),
)
_WHITESPACE = re.compile(r"[ \t\f\v\r]+")
_BLANK_LINES = re.compile(r"\n{3,}")

# Text search configurations per detected language (ISO 639-1); everything else is
# indexed without stemming and stop words.
TS_CONFIGS: Mapping[str, str] = {"de": "german", "en": "english"}
DEFAULT_TS_CONFIG = "simple"


def ts_config_for(language: str | None) -> str:
    return TS_CONFIGS.get((language or "").lower(), DEFAULT_TS_CONFIG)


@dataclass(frozen=True)
class Chunk:
    heading: str
    content: str

    @property
    def text(self) -> str:
        """What is embedded: header context and content."""
        return f"{self.heading}\n\n{self.content}" if self.content else self.heading


def _format_address(address: Mapping[str, Any] | None) -> str:
    if not address:
        return ""
    name = str(address.get("name") or "").strip()
    email = str(address.get("address") or "").strip()
    if name and email:
        return f"{name} <{email}>"
    return name or email


def heading(
    *,
    sender: Mapping[str, Any] | None,
    date: datetime | None,
    subject: str,
    attachment: str | None = None,
) -> str:
    """Header context of a chunk, one ``Field: value`` per line."""
    lines = []
    if sender_text := _format_address(sender):
        lines.append(f"From: {sender_text}")
    if date is not None:
        lines.append(f"Date: {date.date().isoformat()}")
    if subject := " ".join(subject.split()):
        lines.append(f"Subject: {subject}")
    if attachment is not None:
        lines.append(f"Attachment: {' '.join(attachment.split()) or '-'}")
    return "\n".join(lines)


def clean(text: str) -> str:
    """Normalise whitespace: collapse runs of spaces, at most one blank line."""
    lines = (_WHITESPACE.sub(" ", line).strip() for line in text.replace("\r\n", "\n").split("\n"))
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def _split(text: str, size: int, level: int = 0) -> list[str]:
    """Pieces of at most ``size`` characters, cut at the coarsest separator possible.

    Separators stay attached to the piece before them, so joining the pieces gives back
    the text.
    """
    if len(text) <= size:
        return [text]
    if level == len(_SEPARATORS):
        return [text[i : i + size] for i in range(0, len(text), size)]
    pieces: list[str] = []
    start = 0
    for match in _SEPARATORS[level].finditer(text):
        if match.end() > start:
            pieces.extend(_split(text[start : match.end()], size, level + 1))
            start = match.end()
    if start < len(text):
        pieces.extend(_split(text[start:], size, level + 1))
    return pieces


def _tail(text: str, overlap: int) -> str:
    """The last ``overlap`` characters of ``text``, starting at a word boundary."""
    if overlap <= 0 or not text:
        return ""
    if len(text) <= overlap:
        return text
    tail = text[-overlap:]
    space = tail.find(" ")
    return tail[space + 1 :] if 0 <= space < len(tail) - 1 else tail


def split_text(text: str, *, size: int, overlap: int) -> list[str]:
    """Split ``text`` into chunks of at most ``size`` characters with ``overlap``.

    Pieces (paragraphs, lines, sentences, words) are packed greedily; a new chunk starts
    with the end of the previous one.
    """
    text = clean(text)
    if not text:
        return []
    if overlap >= size:
        raise ValueError("overlap must be smaller than size")
    chunks: list[str] = []
    current = ""
    # Room for the overlap of the previous chunk and a separator.
    for piece in _split(text, size - overlap - 1):
        if len(current) + len(piece) <= size:
            current += piece
            continue
        chunks.append(current.strip())
        tail = _tail(current.rstrip(), overlap)
        current = f"{tail} {piece}" if tail else piece
    chunks.append(current.strip())
    return chunks


def chunk_text(text: str, *, context: str, size: int, overlap: int) -> list[Chunk]:
    return [Chunk(context, part) for part in split_text(text, size=size, overlap=overlap)]
