"""Citations in streamed answers and the data blocks the model cites from.

:class:`CitationFilter` sits between the model and the client: it lets through only
markers ``[n]`` whose ``n`` is one of the sources actually passed to the model, rewrites
``[1, 3]`` to ``[1][3]`` and drops every other number. A citation can therefore only point
to a retrieved chunk, whatever the model (or a mail trying to steer it) writes.
"""

import re
import secrets
from collections.abc import Sequence
from dataclasses import dataclass

from app.ai import injection

# A complete marker: ``[1]``, ``[1, 3]``, ``[1,3]``.
_MARKER = re.compile(r"\[\s*(\d{1,3}(?:\s*[,;]\s*\d{1,3})*)\s*\]")
# The start of a marker whose end has not been streamed yet.
_PARTIAL = re.compile(r"\[[\d\s,;]*")
_MAX_MARKER = 32


class CitationFilter:
    """Incremental filter for streamed text; ``cited`` lists valid numbers in order."""

    def __init__(self, sources: int) -> None:
        self._sources = sources
        self._pending = ""
        self.cited: list[int] = []

    def _render(self, numbers: str) -> str:
        out = []
        for part in re.split(r"[,;]", numbers):
            number = int(part)
            if 1 <= number <= self._sources:
                if number not in self.cited:
                    self.cited.append(number)
                out.append(f"[{number}]")
        return "".join(out)

    def feed(self, text: str) -> str:
        buffer = self._pending + text
        self._pending = ""
        out: list[str] = []
        position = 0
        while position < len(buffer):
            start = buffer.find("[", position)
            if start < 0:
                out.append(buffer[position:])
                break
            out.append(buffer[position:start])
            end = buffer.find("]", start)
            if end < 0:
                tail = buffer[start:]
                if len(tail) <= _MAX_MARKER and _PARTIAL.fullmatch(tail):
                    # Wait for the rest of the marker.
                    self._pending = tail
                    break
                out.append("[")
                position = start + 1
                continue
            match = _MARKER.fullmatch(buffer, start, end + 1)
            if match is None:
                out.append("[")
                position = start + 1
                continue
            out.append(self._render(match.group(1)))
            position = end + 1
        return "".join(out)

    def flush(self) -> str:
        """Text held back at the end of the stream (an unfinished marker, kept as text)."""
        rest, self._pending = self._pending, ""
        return rest


def cited_numbers(text: str, sources: int) -> list[int]:
    """Valid citation numbers of a complete text, in order of appearance."""
    citations = CitationFilter(sources)
    citations.feed(text)
    return citations.cited


@dataclass(frozen=True)
class DataBlock:
    number: int
    heading: str
    content: str


def data_tag() -> str:
    """Tag name of the data blocks of one request; unguessable for mail authors."""
    return f"mail-{secrets.token_hex(6)}"


def render_blocks(tag: str, blocks: Sequence[DataBlock], *, feature: str = "rag") -> str:
    """Sources as ``<tag n="1">...</tag>`` blocks. ``tag`` is random per request, so text
    in a mail cannot end its block; the tag is still removed from the content. Passages
    addressed to an AI assistant are removed (#170) and counted for ``feature``."""
    parts = []
    removed = 0
    for block in blocks:
        content = injection.neutralize(block.content)
        removed += content.passages
        body = f"{block.heading}\n\n{content.text}".strip().replace(tag, "mail")
        parts.append(f'<{tag} n="{block.number}">\n{body}\n</{tag}>')
    injection.count(feature, removed)
    return "\n".join(parts)
