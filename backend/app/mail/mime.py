"""MIME parsing and normalisation of RFC 5322 messages.

``parse_message`` turns raw bytes into a ``ParsedMessage``. It never raises on malformed
input: broken headers, unknown or wrong charsets, bad dates and broken transfer encodings
degrade to best-effort values. ``normalize_message`` additionally derives the plain text,
the new content without quotes and signature, and the language.
"""

import codecs
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import policy
from email.headerregistry import Address as HeaderAddress
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any

from app.mail.language import detect_language
from app.mail.quotes import split_reply
from app.mail.sanitize import html_to_text

# Charsets that say "unknown"; decoding falls back to UTF-8 / Windows-1252.
_UNKNOWN_CHARSETS = frozenset({"unknown-8bit", "x-unknown", "unknown", "x-user-defined"})
# Labels that are commonly wrong: real-world "ISO-8859-1" usually is Windows-1252.
_CHARSET_UPGRADES = {
    "iso-8859-1": "cp1252",
    "latin1": "cp1252",
    "us-ascii": "utf-8",
    "ascii": "utf-8",
}
_MESSAGE_ID = re.compile(r"<([^<>\s]+)>")


@dataclass(frozen=True, slots=True)
class Address:
    name: str | None
    address: str

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "address": self.address}


@dataclass(frozen=True, slots=True)
class ParsedAttachment:
    filename: str | None
    content_type: str
    data: bytes = field(repr=False)
    content_id: str | None = None
    is_inline: bool = False


@dataclass(frozen=True, slots=True)
class ParsedMessage:
    message_id: str | None
    in_reply_to: str | None
    references: tuple[str, ...]
    subject: str
    sender: Address | None
    to: tuple[Address, ...]
    cc: tuple[Address, ...]
    bcc: tuple[Address, ...]
    reply_to: tuple[Address, ...]
    sent_at: datetime | None
    headers: tuple[tuple[str, str], ...]
    text: str | None
    html: str | None
    attachments: tuple[ParsedAttachment, ...]
    size: int


@dataclass(frozen=True, slots=True)
class NormalizedMessage:
    parsed: ParsedMessage
    # Full plain text (text/plain part, or converted from HTML).
    body_text: str
    body_main: str
    quoted: str | None
    signature: str | None
    language: str | None


def clean_text(value: str) -> str:
    """Repair surrogate-escaped raw bytes and remove NUL characters (Postgres rejects
    them in text columns)."""
    if any("\udc80" <= char <= "\udcff" for char in value):
        raw = value.encode("utf-8", "surrogateescape")
        value = decode_bytes(raw, None)
    return value.replace("\x00", "")


def _codec(charset: str | None) -> str | None:
    if not charset:
        return None
    name = charset.strip().strip("\"'").lower()
    if name in _UNKNOWN_CHARSETS:
        return None
    name = _CHARSET_UPGRADES.get(name, name)
    try:
        return codecs.lookup(name).name
    except LookupError:
        return None


def decode_bytes(data: bytes, charset: str | None) -> str:
    """Decode with the declared charset; if it is unknown or wrong, try UTF-8 and finally
    Windows-1252 (which maps every byte)."""
    candidates = [c for c in (_codec(charset), "utf-8") if c]
    for candidate in dict.fromkeys(candidates):
        try:
            return data.decode(candidate)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("cp1252", errors="replace")


def _decode_header(name: str, raw: str) -> Any:
    # Raw 8-bit bytes (not RFC 2047 encoded) arrive surrogate-escaped; decode them first
    # so the header parser does not replace them with U+FFFD.
    return policy.default.header_factory(name, clean_text(raw))


def _raw_header(message: Message, name: str) -> str | None:
    wanted = name.lower()
    for key, raw in message.raw_items():
        if key.lower() == wanted:
            return str(raw)
    return None


def _header_str(message: Message, name: str) -> str | None:
    raw = _raw_header(message, name)
    if raw is None:
        return None
    try:
        value = str(_decode_header(name, raw))
    except Exception:
        value = raw
    return " ".join(clean_text(value).split())


def _headers(message: Message) -> tuple[tuple[str, str], ...]:
    headers: list[tuple[str, str]] = []
    for name, raw in message.raw_items():
        try:
            value = str(_decode_header(name, str(raw)))
        except Exception:
            value = str(raw)
        headers.append((name, " ".join(clean_text(value).split())))
    return tuple(headers)


_ADDR_SPEC = re.compile(r"^[^\s@<>\"(),;:]+@[^\s@<>\"(),;:]+$")
_ADDR_SEARCH = re.compile(r"[^\s@<>\"(),;:]+@[^\s@<>\"(),;:]+\.[^\s@<>\"(),;:]+")


def _addresses(message: Message, name: str) -> tuple[Address, ...]:
    raw = _raw_header(message, name)
    if raw is None:
        return ()
    result: list[Address] = []
    try:
        parsed: list[HeaderAddress] = list(getattr(_decode_header(name, raw), "addresses", ()))
        for item in parsed:
            address = clean_text(item.addr_spec)
            if _ADDR_SPEC.match(address):
                result.append(Address(clean_text(item.display_name) or None, address))
    except Exception:
        result = []
    if result:
        return tuple(result)
    # Fallback for headers the strict parser rejects: keep anything address-shaped.
    for display, address in getaddresses([clean_text(raw)]):
        match = _ADDR_SEARCH.search(address) or _ADDR_SEARCH.search(display)
        if match:
            name_part = display if _ADDR_SEARCH.search(display) is None else ""
            result.append(Address(name_part.strip() or None, match.group(0)))
    return tuple(result)


def _message_ids(value: str | None) -> list[str]:
    if not value:
        return []
    found = _MESSAGE_ID.findall(value)
    if found:
        return found
    return [token.strip("<>") for token in value.split() if "@" in token]


def _date(message: Message) -> datetime | None:
    raw = _header_str(message, "date")
    if not raw:
        return None
    try:
        value = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value


def _payload(part: Message) -> bytes:
    try:
        payload = part.get_payload(decode=True)
    except Exception:
        payload = None
    if isinstance(payload, bytes):
        return payload
    raw = part.get_payload()
    return raw.encode("utf-8", "surrogateescape") if isinstance(raw, str) else b""


def _part_text(part: Message) -> str:
    return clean_text(decode_bytes(_payload(part), part.get_content_charset()))


def _filename(part: Message) -> str | None:
    try:
        name = part.get_filename()
    except Exception:
        name = None
    if not name:
        return None
    # Never allow path components: the name is only metadata, but stay defensive.
    name = clean_text(str(name)).replace("\\", "/").rsplit("/", 1)[-1].strip()
    return name or None


def _disposition(part: Message) -> str | None:
    try:
        return part.get_content_disposition()
    except Exception:
        return None


@dataclass(slots=True)
class _Collector:
    texts: list[str] = field(default_factory=list)
    htmls: list[str] = field(default_factory=list)
    attachments: list[ParsedAttachment] = field(default_factory=list)


def _choose_alternative(parts: list[Message], collector: _Collector) -> None:
    """multipart/alternative: keep the best plain and the best HTML representation."""
    plain = [p for p in parts if p.get_content_type() == "text/plain"]
    html = [p for p in parts if p.get_content_type() == "text/html"]
    others = [p for p in parts if p not in plain and p not in html]
    if plain:
        collector.texts.append(_part_text(plain[-1]))
    if html:
        collector.htmls.append(_part_text(html[-1]))
    # Richer alternatives (multipart/related with HTML and images) are walked normally.
    for part in others:
        _walk(part, collector)


def _walk(part: Message, collector: _Collector) -> None:
    content_type = part.get_content_type()
    if part.is_multipart() and not content_type.startswith("message/"):
        children = [p for p in part.get_payload() if isinstance(p, Message)]
        if content_type == "multipart/alternative":
            _choose_alternative(children, collector)
        else:
            for child in children:
                _walk(child, collector)
        return

    disposition = _disposition(part)
    filename = _filename(part)
    is_body_text = (
        content_type in {"text/plain", "text/html"}
        and disposition != "attachment"
        and filename is None
    )
    if is_body_text:
        target = collector.texts if content_type == "text/plain" else collector.htmls
        target.append(_part_text(part))
        return

    if content_type.startswith("message/"):
        # Attached mail (message/rfc822): keep the original bytes as an .eml attachment.
        inner = part.get_payload()
        if isinstance(inner, list) and inner and isinstance(inner[0], Message):
            data = inner[0].as_bytes(policy=policy.SMTP)
        else:
            data = _payload(part)
        collector.attachments.append(
            ParsedAttachment(filename or "attached-message.eml", content_type, data)
        )
        return

    content_id = _header_str(part, "content-id")
    collector.attachments.append(
        ParsedAttachment(
            filename=filename,
            content_type=content_type,
            data=_payload(part),
            content_id=content_id.strip("<> ") if content_id else None,
            is_inline=disposition == "inline" or (disposition is None and content_id is not None),
        )
    )


def parse_message(raw: bytes) -> ParsedMessage:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    collector = _Collector()
    _walk(message, collector)

    sender_list = _addresses(message, "from")
    in_reply_to = _message_ids(_header_str(message, "in-reply-to"))
    message_ids = _message_ids(_header_str(message, "message-id"))
    return ParsedMessage(
        message_id=message_ids[0] if message_ids else None,
        in_reply_to=in_reply_to[0] if in_reply_to else None,
        references=tuple(dict.fromkeys(_message_ids(_header_str(message, "references")))),
        subject=" ".join((_header_str(message, "subject") or "").split()),
        sender=sender_list[0] if sender_list else None,
        to=_addresses(message, "to"),
        cc=_addresses(message, "cc"),
        bcc=_addresses(message, "bcc"),
        reply_to=_addresses(message, "reply-to"),
        sent_at=_date(message),
        headers=_headers(message),
        text="\n\n".join(t for t in collector.texts if t.strip()) or None,
        html="\n".join(collector.htmls) or None,
        attachments=tuple(collector.attachments),
        size=len(raw),
    )


def normalize_message(raw: bytes) -> NormalizedMessage:
    parsed = parse_message(raw)
    body_text = parsed.text if parsed.text else html_to_text(parsed.html or "")
    body_text = body_text.replace("\r\n", "\n").strip()
    parts = split_reply(body_text)
    return NormalizedMessage(
        parsed=parsed,
        body_text=body_text,
        body_main=parts.main,
        quoted=parts.quoted,
        signature=parts.signature,
        language=detect_language(parts.main),
    )
