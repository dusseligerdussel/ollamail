"""IMAP wire format (RFC 3501, RFC 9051): response parsing and argument encoding.

Pure functions without I/O, used by ``imap_client`` and ``imap``.

Parsed values (``Value``):

* ``str``  : an atom or number (``UID``, ``\\Seen``, ``BODY[]``, ``42``), decoded as Latin-1
  so the original bytes can be recovered exactly,
* ``bytes``: a quoted string or literal,
* ``None`` : ``NIL``,
* ``list`` : a parenthesised list.

A response is read from the wire as *segments*: the text of each line, with every literal
that follows a ``{n}`` marker as its own segment (``[text, literal, text, ...]``).
"""

import base64
import bisect
import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from app.mail.providers.base import Flag

Value = str | bytes | None | list["Value"]

# Status responses whose text is free-form (only the response code is parsed).
STATUS_KINDS = frozenset({"OK", "NO", "BAD", "BYE", "PREAUTH"})
# Untagged responses whose data is parsed; everything else is kept as a bare kind.
_PARSED_KINDS = frozenset(
    {
        "CAPABILITY",
        "ENABLED",
        "ESEARCH",
        "EXISTS",
        "EXPUNGE",
        "FETCH",
        "FLAGS",
        "LIST",
        "LSUB",
        "RECENT",
        "SEARCH",
        "STATUS",
        "VANISHED",
    }
)

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_LITERAL_MARKER = re.compile(rb"\{(\d+)\+?\}$")


class ImapParseError(ValueError):
    """The server sent something that is not valid IMAP. Never contains response data."""


@dataclass(frozen=True, slots=True)
class ResponseCode:
    """``[NAME args]`` of a status response, e.g. ``[UIDVALIDITY 3857529045]``."""

    name: str
    args: tuple[Value, ...] = ()


@dataclass(frozen=True, slots=True)
class Untagged:
    # Upper-case response name: ``FETCH``, ``EXISTS``, ``LIST``, ``OK``, ...
    kind: str
    # Message sequence number of ``* 12 FETCH`` / ``* 3 EXPUNGE`` / count of ``* 5 EXISTS``.
    number: int | None = None
    values: tuple[Value, ...] = ()
    code: ResponseCode | None = None


@dataclass(frozen=True, slots=True)
class Tagged:
    tag: str
    status: str
    code: ResponseCode | None = None


@dataclass(frozen=True, slots=True)
class Continuation:
    data: bytes = b""


Response = Untagged | Tagged | Continuation


def literal_size(line: bytes) -> int | None:
    """Size of the literal announced at the end of ``line``, if any."""
    match = _LITERAL_MARKER.search(line)
    return int(match.group(1)) if match else None


class _Reader:
    """Tokenizer over the segments of one response."""

    def __init__(self, segments: Sequence[bytes]) -> None:
        if not segments or len(segments) % 2 == 0:
            raise ImapParseError("incomplete response")
        self._segments = segments
        self._index = 0
        self._pos = 0

    @property
    def _text(self) -> bytes:
        return self._segments[self._index]

    def at_end(self) -> bool:
        self.skip_spaces()
        return self._pos >= len(self._text) and self._index == len(self._segments) - 1

    def skip_spaces(self) -> None:
        text = self._text
        while self._pos < len(text) and text[self._pos] == 0x20:
            self._pos += 1

    def peek(self) -> int | None:
        return self._text[self._pos] if self._pos < len(self._text) else None

    def expect(self, char: bytes) -> None:
        if self.peek() != char[0]:
            raise ImapParseError("unexpected character")
        self._pos += 1

    def rest(self) -> bytes:
        """Remaining text of the current line (free-form status text)."""
        value = self._text[self._pos :]
        self._pos = len(self._text)
        return value

    def value(self) -> Value:
        self.skip_spaces()
        char = self.peek()
        if char is None:
            raise ImapParseError("unexpected end of response")
        if char == ord("("):
            return self._list()
        if char == ord('"'):
            return self._quoted()
        if char == ord("{"):
            return self._literal()
        atom = self.atom()
        return None if atom.upper() == "NIL" else atom

    def _list(self) -> list[Value]:
        self.expect(b"(")
        items: list[Value] = []
        while True:
            self.skip_spaces()
            char = self.peek()
            if char is None:
                raise ImapParseError("unterminated list")
            if char == ord(")"):
                self._pos += 1
                return items
            items.append(self.value())

    def _quoted(self) -> bytes:
        self.expect(b'"')
        text = self._text
        out = bytearray()
        while self._pos < len(text):
            char = text[self._pos]
            self._pos += 1
            if char == ord("\\") and self._pos < len(text):
                out.append(text[self._pos])
                self._pos += 1
            elif char == ord('"'):
                return bytes(out)
            else:
                out.append(char)
        raise ImapParseError("unterminated quoted string")

    def _literal(self) -> bytes:
        text = self._text
        end = text.find(b"}", self._pos)
        if end == -1 or end != len(text) - 1 or self._index + 2 >= len(self._segments):
            raise ImapParseError("literal marker not at end of line")
        literal = self._segments[self._index + 1]
        self._index += 2
        self._pos = 0
        return literal

    def atom(self) -> str:
        """An atom; brackets are kept together (``BODY[HEADER.FIELDS (DATE)]``)."""
        text = self._text
        start = self._pos
        depth = 0
        while self._pos < len(text):
            char = text[self._pos]
            if char == ord("["):
                depth += 1
            elif char == ord("]"):
                if depth == 0:
                    break
                depth -= 1
            elif depth == 0 and char in b' ()"{\r\n':
                break
            self._pos += 1
        if self._pos == start:
            raise ImapParseError("empty atom")
        return text[start : self._pos].decode("latin-1")


def _code(reader: _Reader) -> ResponseCode | None:
    reader.skip_spaces()
    if reader.peek() != ord("["):
        return None
    reader.expect(b"[")
    name = reader.atom().upper()
    args: list[Value] = []
    try:
        while True:
            reader.skip_spaces()
            char = reader.peek()
            if char is None:
                raise ImapParseError("unterminated response code")
            if char == ord("]"):
                break
            args.append(reader.value())
    except ImapParseError:
        # Unknown codes may carry free text; their arguments are not needed.
        args = []
    return ResponseCode(name, tuple(args))


def parse_response(segments: Sequence[bytes]) -> Response:
    """Parse one complete server response."""
    first = segments[0] if segments else b""
    if first.startswith(b"+"):
        return Continuation(first[1:].strip())
    reader = _Reader(segments)
    if first.startswith(b"* "):
        reader.expect(b"*")
        token = reader.value()
        if not isinstance(token, str):
            raise ImapParseError("invalid untagged response")
        number = None
        if token.isdigit():
            number = int(token)
            kind_token = reader.value()
            if not isinstance(kind_token, str):
                raise ImapParseError("invalid untagged response")
            token = kind_token
        kind = token.upper()
        if kind in STATUS_KINDS:
            return Untagged(kind, number, code=_code(reader))
        if kind not in _PARSED_KINDS:
            return Untagged(kind, number)
        values: list[Value] = []
        while not reader.at_end():
            values.append(reader.value())
        return Untagged(kind, number, tuple(values))
    tag = reader.atom()
    reader.skip_spaces()
    status = reader.atom().upper()
    if status not in {"OK", "NO", "BAD"}:
        raise ImapParseError("invalid tagged response")
    return Tagged(tag, status, _code(reader))


# --- Arguments --------------------------------------------------------------------------

_QUOTABLE = re.compile(rb"^[\x01-\x09\x0b\x0c\x0e-\x7f]*$")


def quote(value: bytes) -> bytes | None:
    """``value`` as a quoted string, or ``None`` if it must be sent as a literal."""
    if len(value) > 1000 or not _QUOTABLE.match(value):
        return None
    return b'"' + value.replace(b"\\", b"\\\\").replace(b'"', b'\\"') + b'"'


# --- Mailbox names (modified UTF-7, RFC 3501 §5.1.3) ------------------------------------


def encode_mailbox(name: str) -> bytes:
    out = bytearray()
    pending: list[str] = []

    def flush() -> None:
        if pending:
            data = "".join(pending).encode("utf-16-be")
            encoded = base64.b64encode(data).rstrip(b"=").replace(b"/", b",")
            out.extend(b"&" + encoded + b"-")
            pending.clear()

    for char in name:
        if 0x20 <= ord(char) <= 0x7E:
            flush()
            out.extend(b"&-" if char == "&" else char.encode("ascii"))
        else:
            pending.append(char)
    flush()
    return bytes(out)


def decode_mailbox(raw: bytes) -> str:
    """Decode a mailbox name; servers that send UTF-8 instead are tolerated."""
    if any(byte > 0x7F for byte in raw):
        return raw.decode("utf-8", "replace")
    out: list[str] = []
    pos = 0
    try:
        while pos < len(raw):
            amp = raw.find(b"&", pos)
            if amp == -1:
                out.append(raw[pos:].decode("ascii"))
                break
            out.append(raw[pos:amp].decode("ascii"))
            end = raw.index(b"-", amp)
            chunk = raw[amp + 1 : end]
            if chunk:
                padded = chunk.replace(b",", b"/") + b"=" * (-len(chunk) % 4)
                out.append(base64.b64decode(padded, validate=True).decode("utf-16-be"))
            else:
                out.append("&")
            pos = end + 1
    except (ValueError, UnicodeDecodeError):
        return raw.decode("ascii", "replace")
    return "".join(out)


# --- UID sets ---------------------------------------------------------------------------


class UidSet:
    """Immutable set of UIDs stored as sorted, disjoint ranges (compact for cursors)."""

    __slots__ = ("_ranges", "_starts")

    def __init__(self, ranges: Iterable[tuple[int, int]] = ()) -> None:
        merged: list[tuple[int, int]] = []
        for start, end in sorted((min(a, b), max(a, b)) for a, b in ranges):
            if merged and start <= merged[-1][1] + 1:
                if end > merged[-1][1]:
                    merged[-1] = (merged[-1][0], end)
            else:
                merged.append((start, end))
        self._ranges = tuple(merged)
        self._starts = [start for start, _ in merged]

    @classmethod
    def of(cls, uids: Iterable[int]) -> "UidSet":
        return cls((uid, uid) for uid in uids)

    @classmethod
    def parse(cls, value: str) -> "UidSet":
        """Parse a sequence set without ``*`` (``1:5,7,10:12``)."""
        if not value:
            return cls()
        ranges = []
        try:
            for part in value.split(","):
                start, _, end = part.partition(":")
                ranges.append((int(start), int(end or start)))
        except ValueError:
            raise ImapParseError("invalid sequence set") from None
        if any(start < 1 or end < 1 for start, end in ranges):
            raise ImapParseError("invalid sequence set")
        return cls(ranges)

    def __str__(self) -> str:
        return ",".join(f"{a}" if a == b else f"{a}:{b}" for a, b in self._ranges)

    def __repr__(self) -> str:
        return f"UidSet({str(self)!r})"

    def __bool__(self) -> bool:
        return bool(self._ranges)

    def __len__(self) -> int:
        return sum(end - start + 1 for start, end in self._ranges)

    def __iter__(self) -> Iterator[int]:
        for start, end in self._ranges:
            yield from range(start, end + 1)

    def __contains__(self, uid: object) -> bool:
        if not isinstance(uid, int):
            return False
        index = bisect.bisect_right(self._starts, uid) - 1
        return index >= 0 and uid <= self._ranges[index][1]

    def __eq__(self, other: object) -> bool:
        return isinstance(other, UidSet) and self._ranges == other._ranges

    def __hash__(self) -> int:
        return hash(self._ranges)

    @property
    def min(self) -> int:
        return self._ranges[0][0]

    @property
    def max(self) -> int:
        return self._ranges[-1][1]

    def newest(self, count: int) -> "UidSet":
        """The ``count`` highest UIDs (all if there are fewer)."""
        ranges: list[tuple[int, int]] = []
        for start, end in reversed(self._ranges):
            if count <= 0:
                break
            first = max(start, end - count + 1)
            ranges.append((first, end))
            count -= end - first + 1
        return UidSet(ranges)

    def union(self, other: "UidSet | Iterable[int]") -> "UidSet":
        extra = other._ranges if isinstance(other, UidSet) else ((u, u) for u in other)
        return UidSet((*self._ranges, *extra))

    def difference(self, uids: Iterable[int]) -> "UidSet":
        removed = set(uids)
        if not removed:
            return self
        return UidSet.of(uid for uid in self if uid not in removed)


# --- Flags ------------------------------------------------------------------------------

_SYSTEM_FLAGS = {
    "\\SEEN": Flag.SEEN,
    "\\ANSWERED": Flag.ANSWERED,
    "\\FLAGGED": Flag.FLAGGED,
    "\\DRAFT": Flag.DRAFT,
    "\\DELETED": Flag.DELETED,
}
_ATOM_SPECIALS = set('(){ %*"\\]')


def _imap_system_flag(flag: Flag) -> str:
    return "\\" + flag.value.capitalize()


def flags_from_imap(values: Iterable[Value]) -> frozenset[str]:
    """Map IMAP flags to ``Flag`` values; keywords are kept, other system flags dropped."""
    flags: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        system = _SYSTEM_FLAGS.get(value.upper())
        if system is not None:
            flags.add(system.value)
        elif not value.startswith("\\"):
            flags.add(value)
    return frozenset(flags)


def encode_keyword(label: str) -> str:
    """A label as IMAP keyword (an atom). Valid atoms are kept; otherwise non-ASCII is
    encoded like mailbox names and spaces become ``_``, so the mapping is deterministic."""
    if label and all(0x21 <= ord(c) <= 0x7E and c not in _ATOM_SPECIALS for c in label):
        return label
    encoded = encode_mailbox(label).decode("ascii").replace(" ", "_")
    keyword = "".join(c for c in encoded if 0x21 <= ord(c) <= 0x7E and c not in _ATOM_SPECIALS)
    if not keyword:
        raise ValueError("label cannot be expressed as IMAP keyword")
    return keyword


def flags_to_imap(flags: Iterable[str]) -> list[str]:
    result = []
    for flag in sorted(set(flags)):
        try:
            result.append(_imap_system_flag(Flag(flag)))
        except ValueError:
            result.append(encode_keyword(flag))
    return result


# --- Dates ------------------------------------------------------------------------------


def format_search_date(value: date) -> str:
    """``SINCE`` argument, e.g. ``1-Feb-1994``."""
    return f"{value.day}-{_MONTHS[value.month - 1]}-{value.year}"


_INTERNALDATE = re.compile(
    r"^\s*(\d{1,2})-([A-Za-z]{3})-(\d{4}) (\d{2}):(\d{2}):(\d{2}) ([+-])(\d{2})(\d{2})$"
)


def parse_internaldate(value: Value) -> datetime | None:
    """Parse ``INTERNALDATE`` (``17-Jul-1996 02:44:25 -0700``); ``None`` if invalid."""
    if isinstance(value, bytes):
        value = value.decode("ascii", "replace")
    if not isinstance(value, str):
        return None
    match = _INTERNALDATE.match(value)
    if not match:
        return None
    day, month, year, hour, minute, second, sign, tz_hour, tz_minute = match.groups()
    try:
        month_index = [m.lower() for m in _MONTHS].index(month.lower()) + 1
        offset = timedelta(hours=int(tz_hour), minutes=int(tz_minute))
        return datetime(
            int(year),
            month_index,
            int(day),
            int(hour),
            int(minute),
            int(second),
            tzinfo=timezone(-offset if sign == "-" else offset),
        )
    except ValueError:
        return None


# --- FETCH ------------------------------------------------------------------------------


def fetch_items(response: Untagged) -> dict[str, Value]:
    """The data items of a ``FETCH`` response, keyed by upper-case name (``BODY[]``)."""
    if response.kind != "FETCH" or not response.values or not isinstance(response.values[0], list):
        raise ImapParseError("invalid FETCH response")
    data = response.values[0]
    if len(data) % 2:
        raise ImapParseError("invalid FETCH response")
    items: dict[str, Value] = {}
    for index in range(0, len(data), 2):
        key = data[index]
        if not isinstance(key, str):
            raise ImapParseError("invalid FETCH response")
        name = key.upper()
        # ``BODY[]<0>`` (partial fetch) is reported with the origin.
        if "<" in name and name.endswith(">"):
            name = name[: name.index("<")]
        items[name] = data[index + 1]
    return items


def as_int(value: Value) -> int | None:
    if isinstance(value, list):
        value = value[0] if len(value) == 1 else None
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None
