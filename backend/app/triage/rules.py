"""Rule-based pre-filter: decides obvious cases without the model.

Order: the user's sender rules first (exact address, then domain), then header rules:

* ``Auto-Submitted`` other than ``no`` (RFC 3834: auto-replies, system mail) and
  well-known robot senders (``no-reply@``, ``mailer-daemon@``, ...) → Notification
* ``List-Unsubscribe`` or ``Precedence: bulk|list`` → Newsletter
* ``Precedence: junk`` → Spam/Advertising

A header rule only applies if the user has the built-in target category and has not
hidden it; otherwise the model decides. The functions are pure and never log.
"""

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from app.triage.categories import EffectiveCategory
from app.triage.models import TriageSource

# Local parts of senders that never are a person.
_ROBOT_SENDER = re.compile(
    r"^(no[-_.]?reply|do[-_.]?not[-_.]?reply|mailer[-_.]daemon|postmaster|bounces?"
    r"|notifications?|notify|alerts?)([-+_.].*)?$"
)
LOW_PRIORITY = 3


@dataclass(frozen=True, slots=True)
class SenderRule:
    # Lower-case address or ``@domain``.
    sender: str
    category_id: uuid.UUID
    priority: int


@dataclass(frozen=True, slots=True)
class RuleDecision:
    category_id: uuid.UUID
    priority: int
    source: TriageSource
    # Machine-readable rule name, stored with the result.
    rule: str


def header_values(headers: Sequence[Sequence[str]], name: str) -> list[str]:
    wanted = name.lower()
    return [pair[1] for pair in headers if len(pair) == 2 and pair[0].lower() == wanted]


def normalize_sender(address: str) -> str:
    return address.strip().lower()


def sender_domain(address: str) -> str | None:
    _, at, domain = normalize_sender(address).rpartition("@")
    return f"@{domain}" if at and domain else None


def match_sender_rule(address: str | None, rules: Sequence[SenderRule]) -> SenderRule | None:
    if not address:
        return None
    by_sender = {rule.sender: rule for rule in rules}
    exact = by_sender.get(normalize_sender(address))
    if exact is not None:
        return exact
    domain = sender_domain(address)
    return by_sender.get(domain) if domain else None


def _header_rule(headers: Sequence[Sequence[str]], address: str | None) -> tuple[str, str] | None:
    """``(builtin category key, rule name)`` of the first matching header rule."""
    auto = [v.strip().lower() for v in header_values(headers, "Auto-Submitted")]
    if any(value and not value.startswith("no") for value in auto):
        return "notification", "auto_submitted"
    if address:
        local = normalize_sender(address).partition("@")[0]
        if _ROBOT_SENDER.match(local):
            return "notification", "robot_sender"
    precedence = {v.strip().lower() for v in header_values(headers, "Precedence")}
    if "junk" in precedence:
        return "spam", "precedence_junk"
    if header_values(headers, "List-Unsubscribe"):
        return "newsletter", "list_unsubscribe"
    if precedence & {"bulk", "list"}:
        return "newsletter", "precedence_bulk"
    return None


def prefilter(
    headers: Sequence[Sequence[str]],
    sender_address: str | None,
    sender_rules: Sequence[SenderRule],
    categories: Sequence[EffectiveCategory],
) -> RuleDecision | None:
    """A decision without the model, or ``None`` if the model has to decide.

    ``categories`` are the visible categories of the user.
    """
    visible = {category.id for category in categories}
    rule = match_sender_rule(sender_address, sender_rules)
    if rule is not None and rule.category_id in visible:
        return RuleDecision(rule.category_id, rule.priority, TriageSource.SENDER_RULE, "sender")
    match = _header_rule(headers, sender_address)
    if match is None:
        return None
    builtin_key, name = match
    for category in categories:
        if category.builtin_key == builtin_key:
            return RuleDecision(category.id, LOW_PRIORITY, TriageSource.RULE, name)
    return None
