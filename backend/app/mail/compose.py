"""Compose replies: recipients, subject, threading headers and the RFC 5322 source.

Shared by all providers (``OutgoingReply.raw``), so every reply carries the same headers:

* ``In-Reply-To`` is the ``Message-ID`` of the answered mail; ``References`` is its
  ``References`` plus that ``Message-ID`` (RFC 5322 §3.6.4), shortened to the first and the
  newest entries.
* The subject gets one ``Re:``; existing reply prefixes (``Re:``, ``AW:``, ``Antw:``,
  ``SV:``, ...) are folded into it.
* Reply goes to ``Reply-To``, else to the sender; replying to an own mail (the sender is
  the mailbox) goes to its recipients again. Reply all adds the other recipients as
  ``Cc``, without the mailbox's own address and without duplicates.

Header values are set through ``email.headerregistry``; addresses that do not parse and
values with line breaks are rejected (no header injection).
"""

import re
from collections.abc import Iterable, Sequence
from datetime import datetime
from email.headerregistry import Address
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime, make_msgid
from typing import Any

from app.mail.providers.base import OutgoingAddress

# Entries kept in ``References``: the first (thread root) and the newest ones.
MAX_REFERENCES = 20
_REPLY_PREFIX = re.compile(
    r"^\s*(?:(?:re|aw|antw|sv|vs|ref|rif|odp|wg)\s*(?:\[\d+\]|\(\d+\))?\s*:\s*)+",
    re.IGNORECASE,
)
_ATTRIBUTION = {
    "en": "On {date}, {sender} wrote:",
    "de": "Am {date} schrieb {sender}:",
}


class InvalidAddressError(ValueError):
    """An address or display name cannot be used in a header."""


def reply_subject(subject: str) -> str:
    """``Re: <subject>`` with earlier reply prefixes removed."""
    rest = _REPLY_PREFIX.sub("", subject or "").strip()
    return f"Re: {rest}" if rest else "Re:"


def _addresses(values: Iterable[Any]) -> list[OutgoingAddress]:
    result = []
    for value in values:
        if isinstance(value, dict) and isinstance(value.get("address"), str) and value["address"]:
            name = value.get("name")
            result.append(OutgoingAddress(address=value["address"], name=name if name else None))
    return result


def _unique(
    addresses: Iterable[OutgoingAddress], exclude: Iterable[str] = ()
) -> list[OutgoingAddress]:
    seen = {a.lower() for a in exclude}
    result = []
    for address in addresses:
        key = address.address.lower()
        if key not in seen:
            seen.add(key)
            result.append(address)
    return result


def reply_recipients(
    *,
    sender: dict[str, Any] | None,
    to: Sequence[dict[str, Any]],
    cc: Sequence[dict[str, Any]],
    reply_to: Sequence[dict[str, Any]],
    own_addresses: Iterable[str],
    reply_all: bool,
) -> tuple[list[OutgoingAddress], list[OutgoingAddress]]:
    """``(to, cc)`` of a reply to a mail with the given header addresses."""
    own = {a.lower() for a in own_addresses}
    from_ = _addresses([sender] if sender else [])
    own_mail = bool(from_) and from_[0].address.lower() in own
    primary = _addresses(to) if own_mail else _addresses(reply_to) or from_
    recipients = _unique(primary)
    # Not to the mailbox itself, unless nobody else is left (a note to self).
    recipients = [a for a in recipients if a.address.lower() not in own] or recipients
    copies: list[OutgoingAddress] = []
    if reply_all:
        others = _addresses(cc) if own_mail else [*_addresses(to), *_addresses(cc)]
        copies = _unique(others, exclude=[*own, *(a.address for a in recipients)])
    return recipients, copies


def references(message_id: str | None, earlier: Sequence[str]) -> list[str]:
    """``References`` of a reply to the mail ``message_id`` with ``References`` ``earlier``."""
    chain = [ref for ref in earlier if ref]
    if message_id and message_id not in chain:
        chain.append(message_id)
    if len(chain) > MAX_REFERENCES:
        chain = [chain[0], *chain[-(MAX_REFERENCES - 1) :]]
    return chain


def _bracketed(message_id: str) -> str:
    message_id = message_id.strip()
    return message_id if message_id.startswith("<") else f"<{message_id}>"


def quote(text: str, *, sender: str, sent_at: datetime | None, language: str | None) -> str:
    """The answered mail as quoted text (``> `` lines) with an attribution line."""
    lang = (language or "en").split("-")[0].lower()
    template = _ATTRIBUTION.get(lang, _ATTRIBUTION["en"])
    date = sent_at.strftime("%Y-%m-%d %H:%M") if sent_at else "?"
    lines = [f"> {line}" if line else ">" for line in text.strip().splitlines()]
    return template.format(date=date, sender=sender) + "\n" + "\n".join(lines)


def _header_address(address: OutgoingAddress) -> Address:
    name = address.name or ""
    if "\r" in name or "\n" in name or "\r" in address.address or "\n" in address.address:
        raise InvalidAddressError("line break in address")
    try:
        parsed = Address(display_name=name, addr_spec=address.address)
    except (ValueError, IndexError):
        raise InvalidAddressError("invalid address") from None
    if not parsed.username or not parsed.domain:
        raise InvalidAddressError("invalid address")
    return parsed


def validate_address(address: OutgoingAddress) -> None:
    """Raise ``InvalidAddressError`` if ``address`` cannot be used in a header."""
    _header_address(address)


def build_reply(
    *,
    sender: OutgoingAddress,
    to: Sequence[OutgoingAddress],
    cc: Sequence[OutgoingAddress],
    subject: str,
    body: str,
    in_reply_to: str | None,
    references: Sequence[str],
    date: datetime,
) -> tuple[bytes, str]:
    """The RFC 5322 source of a plain-text reply and its ``Message-ID``."""
    if "\r" in subject or "\n" in subject:
        raise InvalidAddressError("line break in subject")
    message = EmailMessage(policy=SMTP)
    message["From"] = _header_address(sender)
    message["To"] = [_header_address(a) for a in to]
    if cc:
        message["Cc"] = [_header_address(a) for a in cc]
    message["Subject"] = subject
    message["Date"] = format_datetime(date)
    domain = sender.address.rpartition("@")[2] or None
    message_id = make_msgid(domain=domain)
    message["Message-ID"] = message_id
    if in_reply_to:
        message["In-Reply-To"] = _bracketed(in_reply_to)
    if references:
        message["References"] = " ".join(_bracketed(ref) for ref in references)
    message.set_content(body.replace("\r\n", "\n"), charset="utf-8")
    return message.as_bytes(policy=SMTP), message_id
