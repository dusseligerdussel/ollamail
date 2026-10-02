"""The part of the SCIM filter language (RFC 7644 §3.4.2.2) Entra ID and Okta use.

Supported: ``eq`` comparisons, joined with ``and``, on single attributes or one level of
sub-attributes, including the value-path form Entra ID sends to check a membership::

    userName eq "erika@example.org"
    externalId eq "4bd2..." and userName eq "x"
    id eq "0192..." and members[value eq "0192..."]

Attribute names are case-insensitive and may carry the core schema URN. Anything else
(``or``, ``not``, ``co``, ``sw``, ``pr`` ...) is answered with 400 ``invalidFilter`` instead of
being guessed at, so an IdP never acts on a wrong match.
"""

import json
import re
from dataclasses import dataclass

from app.scim.errors import ScimError

_MAX_FILTER_LENGTH = 1024
_CORE_USER = "urn:ietf:params:scim:schemas:core:2.0:user:"
_CORE_GROUP = "urn:ietf:params:scim:schemas:core:2.0:group:"

_TOKEN = re.compile(
    r"""\s*(?:
        (?P<string>"(?:[^"\\]|\\.)*")
      | (?P<open>\[) | (?P<close>\])
      | (?P<word>[A-Za-z0-9_:.$-]+)
    )""",
    re.VERBOSE,
)
_LITERALS: dict[str, str | bool | None] = {"true": True, "false": False, "null": None}


@dataclass(frozen=True)
class Condition:
    """``attribute eq value``; ``attribute`` is lower case, sub-attributes joined by dots."""

    attribute: str
    value: str | bool | None


def invalid_filter(detail: str = "The filter is not supported.") -> ScimError:
    return ScimError(400, detail, "invalidFilter")


def normalize_path(path: str) -> str:
    """Lower-case attribute path without the core schema URN."""
    lowered = path.strip().lower()
    for prefix in (_CORE_USER, _CORE_GROUP):
        if lowered.startswith(prefix):
            return lowered[len(prefix) :]
    return lowered


def _tokens(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    position = 0
    text = text.rstrip()
    while position < len(text):
        match = _TOKEN.match(text, position)
        if match is None or match.end() == position:
            raise invalid_filter()
        kind = match.lastgroup
        assert kind is not None
        tokens.append((kind, match.group(kind)))
        position = match.end()
    return tokens


def _value(kind: str, raw: str) -> str | bool | None:
    if kind == "string":
        try:
            value = json.loads(raw)
        except ValueError:
            raise invalid_filter() from None
        if not isinstance(value, str):
            raise invalid_filter()
        return value
    if kind == "word" and raw.lower() in _LITERALS:
        return _LITERALS[raw.lower()]
    raise invalid_filter()


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]]) -> None:
        self.tokens = tokens
        self.position = 0

    def next(self) -> tuple[str, str]:
        if self.position >= len(self.tokens):
            raise invalid_filter()
        token = self.tokens[self.position]
        self.position += 1
        return token

    def done(self) -> bool:
        return self.position >= len(self.tokens)

    def word(self) -> str:
        kind, raw = self.next()
        if kind != "word":
            raise invalid_filter()
        return raw

    def comparison(self, prefix: str = "") -> Condition:
        attribute = prefix + normalize_path(self.word())
        if self.word().lower() != "eq":
            raise invalid_filter()
        kind, raw = self.next()
        return Condition(attribute, _value(kind, raw))

    def term(self) -> Condition:
        attribute = normalize_path(self.word())
        if not self.done() and self.tokens[self.position][0] == "open":
            # Value path: members[value eq "..."]
            self.position += 1
            inner = self.comparison(prefix=attribute + ".")
            if self.next()[0] != "close":
                raise invalid_filter()
            return inner
        self.position -= 1
        return self.comparison()

    def conditions(self) -> list[Condition]:
        conditions = [self.term()]
        while not self.done():
            if self.word().lower() != "and":
                raise invalid_filter()
            conditions.append(self.term())
        return conditions


def parse_filter(text: str | None, supported: frozenset[str]) -> list[Condition]:
    """The conditions of ``text`` (all must hold); empty without a filter."""
    if text is None or not text.strip():
        return []
    if len(text) > _MAX_FILTER_LENGTH:
        raise invalid_filter("The filter is too long.")
    conditions = _Parser(_tokens(text)).conditions()
    for condition in conditions:
        if condition.attribute not in supported:
            raise invalid_filter("Filtering on this attribute is not supported.")
    return conditions
