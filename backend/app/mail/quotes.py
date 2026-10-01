"""Split a plain-text mail body into new content, quoted history and signature.

Heuristics cover the common clients in German and English: ``>`` quoting, "On … wrote:" /
"Am … schrieb …:" attribution lines (Gmail, Apple Mail, Thunderbird), Outlook header
blocks ("From:/Sent:" or "Von:/Gesendet:", optionally after a line of underscores),
"Original Message" separators, the RFC 3676 signature delimiter ``-- `` and mobile
signatures ("Sent from my iPhone").
"""

import re
from dataclasses import dataclass

_ATTRIBUTION = [
    re.compile(r"^on\b.{1,250}\bwrote:\s*$", re.IGNORECASE),
    re.compile(r"^am\b.{1,250}\bschrieb\b.{0,250}:\s*$", re.IGNORECASE),
    re.compile(r"^le\b.{1,250}\ba écrit\s*:\s*$", re.IGNORECASE),
    re.compile(r"^el\b.{1,250}\bescribió:\s*$", re.IGNORECASE),
]
_SEPARATOR = re.compile(
    r"^\s*-{2,}\s*(original message|ursprüngliche nachricht|forwarded message|"
    r"weitergeleitete nachricht|nachricht im original)\s*-{2,}\s*$",
    re.IGNORECASE,
)
_UNDERSCORES = re.compile(r"^\s*_{10,}\s*$")
_HEADER_FROM = re.compile(r"^\s*\**(from|von|de|van)\s*:\**\s", re.IGNORECASE)
_HEADER_FOLLOW = re.compile(
    r"^\s*\**(sent|date|gesendet|datum|to|an|subject|betreff|envoyé|enviado)\s*:\**\s",
    re.IGNORECASE,
)
_MOBILE_SIGNATURE = re.compile(
    r"^\s*(sent from my \w+|von meinem \w+ gesendet|gesendet von meinem \w+|"
    r"get outlook for \w+|outlook für \w+ beziehen|sent from (mail|outlook) for \w+|"
    r"von (mail|outlook) für \w+ gesendet)\b.{0,60}$",
    re.IGNORECASE,
)
# A signature after "-- " is at most this many lines long; longer tails are content.
_MAX_SIGNATURE_LINES = 20


@dataclass(frozen=True, slots=True)
class ReplyParts:
    main: str
    quoted: str | None
    signature: str | None


def _is_quoted(line: str) -> bool:
    return line.lstrip().startswith(">")


def _is_attribution(lines: list[str], index: int) -> bool:
    line = lines[index].strip()
    if any(pattern.match(line) for pattern in _ATTRIBUTION):
        return True
    # Attribution wrapped over two lines ("On Mon, 1 Jan 2026 at 10:00, Name\n<a@b> wrote:").
    if index + 1 < len(lines) and re.match(r"^(on|am)\b", line, re.IGNORECASE):
        joined = f"{line} {lines[index + 1].strip()}"
        return any(pattern.match(joined) for pattern in _ATTRIBUTION)
    return False


def _is_outlook_header(lines: list[str], index: int) -> bool:
    if not _HEADER_FROM.match(lines[index]):
        return False
    following = [line for line in lines[index + 1 : index + 5] if line.strip()]
    return any(_HEADER_FOLLOW.match(line) for line in following)


def _quote_start(lines: list[str]) -> int | None:
    for index, line in enumerate(lines):
        if _SEPARATOR.match(line) or _is_attribution(lines, index):
            return index
        if _UNDERSCORES.match(line) and index + 1 < len(lines):
            rest = [i for i in range(index + 1, min(index + 4, len(lines))) if lines[i].strip()]
            if rest and _is_outlook_header(lines, rest[0]):
                return index
        if index > 0 and _is_outlook_header(lines, index):
            return index
    # Otherwise only a trailing block of ">" lines counts as quote (inline replies stay).
    start = None
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index]
        if _is_quoted(line):
            start = index
        elif line.strip():
            break
    return start


def _signature_start(lines: list[str]) -> int | None:
    for index in range(len(lines) - 1, max(-1, len(lines) - 1 - _MAX_SIGNATURE_LINES), -1):
        if lines[index].rstrip() == "--":
            return index
    non_empty = [i for i, line in enumerate(lines) if line.strip()]
    if non_empty and _MOBILE_SIGNATURE.match(lines[non_empty[-1]]):
        return non_empty[-1]
    return None


def _join(lines: list[str]) -> str:
    return "\n".join(lines).strip("\n").rstrip()


def split_reply(text: str) -> ReplyParts:
    lines = text.replace("\r\n", "\n").split("\n")

    quoted: str | None = None
    start = _quote_start(lines)
    if start is not None:
        quoted = _join(lines[start:]) or None
        lines = lines[:start]

    signature: str | None = None
    sig_start = _signature_start(lines)
    if sig_start is not None:
        tail = lines[sig_start + 1 :] if lines[sig_start].rstrip() == "--" else lines[sig_start:]
        signature = _join(tail) or None
        lines = lines[:sig_start]

    main = _join(lines)
    if not main and quoted and start == 0 and not signature:
        # Nothing but a quote (e.g. a forwarded mail without comment): keep it readable.
        return ReplyParts(main=quoted, quoted=None, signature=None)
    return ReplyParts(main=main, quoted=quoted, signature=signature)
