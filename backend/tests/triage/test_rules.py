"""Pre-filter without LLM."""

import uuid

import pytest

from app.triage.categories import DEFAULT_CATEGORIES, EffectiveCategory
from app.triage.models import TriageSource
from app.triage.rules import SenderRule, match_sender_rule, prefilter


def _categories(*hidden: str) -> list[EffectiveCategory]:
    return [
        EffectiveCategory(
            id=uuid.uuid4(),
            key=d.key,
            name=d.name,
            description=d.description,
            builtin_key=d.key,
            owner_user_id=None,
            position=i,
            hidden=False,
        )
        for i, d in enumerate(DEFAULT_CATEGORIES)
        if d.key not in hidden
    ]


def _key(categories: list[EffectiveCategory], category_id: uuid.UUID) -> str:
    return next(c.key for c in categories if c.id == category_id)


@pytest.mark.parametrize(
    ("headers", "sender", "expected", "rule"),
    [
        ([("List-Unsubscribe", "<mailto:u@x.org>")], "n@x.org", "newsletter", "list_unsubscribe"),
        ([("Precedence", "bulk")], "news@example.org", "newsletter", "precedence_bulk"),
        ([("precedence", " List ")], "list@example.org", "newsletter", "precedence_bulk"),
        ([("Precedence", "junk")], "offers@example.org", "spam", "precedence_junk"),
        ([("Auto-Submitted", "auto-replied")], "max@example.org", "notification", "auto_submitted"),
        ([("Auto-Submitted", "auto-generated")], "b@x.org", "notification", "auto_submitted"),
        ([], "no-reply@shop.example.org", "notification", "robot_sender"),
        ([], "noreply+abc@example.org", "notification", "robot_sender"),
        ([], "MAILER-DAEMON@example.org", "notification", "robot_sender"),
        # Auto-Submitted wins over List-Unsubscribe.
        (
            [("List-Unsubscribe", "<https://example.org/u>"), ("Auto-Submitted", "auto-generated")],
            "alerts@example.org",
            "notification",
            "auto_submitted",
        ),
    ],
)  # fmt: skip
def test_header_rules(
    headers: list[tuple[str, str]], sender: str, expected: str, rule: str
) -> None:
    categories = _categories()

    decision = prefilter(headers, sender, [], categories)

    assert decision is not None
    assert _key(categories, decision.category_id) == expected
    assert decision.priority == 3
    assert decision.source == TriageSource.RULE
    assert decision.rule == rule


@pytest.mark.parametrize(
    ("headers", "sender"),
    [
        ([], "colleague@example.org"),
        ([("Auto-Submitted", "no")], "colleague@example.org"),
        ([("Precedence", "first-class")], "colleague@example.org"),
        # A mailing-list ID alone may be a discussion with the recipient.
        ([("List-Id", "<team.example.org>")], "colleague@example.org"),
        ([], None),
    ],
)
def test_no_rule_leaves_it_to_the_model(headers: list[tuple[str, str]], sender: str | None) -> None:
    assert prefilter(headers, sender, [], _categories()) is None


def test_hidden_target_category_falls_through_to_the_model() -> None:
    categories = _categories("newsletter")

    assert prefilter([("List-Unsubscribe", "<x>")], "n@example.org", [], categories) is None


def test_sender_rule_beats_header_rules() -> None:
    categories = _categories()
    important = next(c for c in categories if c.key == "important")
    rules = [SenderRule("boss@example.org", important.id, 1)]

    decision = prefilter([("Precedence", "bulk")], "Boss@Example.org", rules, categories)

    assert decision is not None
    assert decision.category_id == important.id
    assert decision.priority == 1
    assert decision.source == TriageSource.SENDER_RULE


def test_sender_rule_for_hidden_category_is_ignored() -> None:
    categories = _categories()
    rules = [SenderRule("a@example.org", uuid.uuid4(), 2)]

    assert prefilter([], "a@example.org", rules, categories) is None


def test_exact_address_beats_domain_rule() -> None:
    domain = SenderRule("@example.org", uuid.uuid4(), 3)
    exact = SenderRule("ceo@example.org", uuid.uuid4(), 1)

    assert match_sender_rule("CEO@example.org", [domain, exact]) == exact
    assert match_sender_rule("other@example.org", [domain, exact]) == domain
    assert match_sender_rule("x@sub.example.org", [domain, exact]) is None
    assert match_sender_rule(None, [domain]) is None
