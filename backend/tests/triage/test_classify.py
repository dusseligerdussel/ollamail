"""Prompt building and the structured LLM call (fake provider)."""

import json
import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.ai.llm import LLMOutputError
from app.triage.categories import EffectiveCategory, slugify
from app.triage.classify import (
    Example,
    MailView,
    build_messages,
    classify,
    clean_text,
    decision_schema,
    mail_view,
)
from tests.triage.conftest import FakeLLM, answer


def _category(key: str, description: str = "") -> EffectiveCategory:
    return EffectiveCategory(
        id=uuid.uuid4(),
        key=key,
        name=key.title(),
        description=description,
        builtin_key=None,
        owner_user_id=None,
        position=0,
        hidden=False,
    )


CATEGORIES = [
    _category("important", "Mail that matters."),
    _category("newsletter", "Subscribed bulk mail."),
]

VIEW = MailView(
    subject="Contract renewal",
    sender_name="Erika Beispiel",
    sender_address="erika@example.org",
    addressed="to",
    date=datetime(2026, 9, 30, 9, 0, tzinfo=UTC),
    body="Please sign the attached contract by Friday.",
)


@pytest.mark.parametrize(
    ("name", "key"),
    [
        ("Projekt Übersicht", "projekt_ubersicht"),
        ("  Spam/Werbung!! ", "spam_werbung"),
        ("日本", "category"),
        ("x" * 50, "x" * 32),
    ],
)
def test_slugify(name: str, key: str) -> None:
    assert slugify(name) == key


def test_clean_text_normalises_whitespace_and_cuts() -> None:
    assert clean_text("a  \t b\r\n\n\n\nc ", 100) == "a b\n\nc"
    assert clean_text("abcdef", 3) == "abc …"


def test_mail_view_knows_how_the_recipient_was_addressed() -> None:
    def view(to: list[str], cc: list[str]) -> MailView:
        return mail_view(
            subject="S",
            sender={"name": None, "address": "a@example.org"},
            to=[{"address": a} for a in to],
            cc=[{"address": a} for a in cc],
            mailbox_address="Me@example.org",
            date=None,
            body_main="",
            body_text="fallback body",
            body_chars=100,
        )

    assert view(["me@example.org"], []).addressed == "to"
    assert view([], ["ME@example.org"]).addressed == "cc"
    assert view(["list@example.org"], []).addressed == "other"
    assert view([], []).body == "fallback body"


def test_prompt_lists_categories_and_wraps_the_mail() -> None:
    system, user = build_messages(VIEW, CATEGORIES, [], "de-AT")

    assert system.role == "system" and user.role == "user"
    assert "- important: Important. Mail that matters." in system.content
    assert "- newsletter: Newsletter. Subscribed bulk mail." in system.content
    # Language of the reason follows the requested language.
    assert "auf Deutsch" in system.content
    assert "Frühere Entscheidungen" not in system.content
    assert user.content.startswith("E-Mail:\n<<<\nFrom: Erika Beispiel <erika@example.org>")
    assert "Recipient addressed: directly (To)" in user.content
    assert user.content.endswith("Please sign the attached contract by Friday.\n>>>")


def test_prompt_contains_examples() -> None:
    example = Example(
        MailView("Weekly digest", None, "digest@example.org", "other", None, "Top news"),
        "newsletter",
        3,
    )

    system, _ = build_messages(VIEW, CATEGORIES, [example], "en")

    assert "Earlier decisions of the recipient" in system.content
    assert (
        "- From: digest@example.org | Subject: Weekly digest | Text: Top news "
        "→ category=newsletter, priority=3" in system.content
    )


def test_mail_content_with_template_syntax_is_inserted_verbatim() -> None:
    view = MailView("$subject ${x}", None, None, "other", None, "Price: $5 {braces}")

    _, user = build_messages(view, CATEGORIES, [], "en")

    assert "Subject: $subject ${x}" in user.content
    assert "Price: $5 {braces}" in user.content


def test_decision_schema_restricts_category_and_priority() -> None:
    schema = decision_schema(["important", "newsletter"])

    assert schema.model_json_schema()["properties"]["category"]["enum"] == [
        "important",
        "newsletter",
    ]
    assert schema.model_validate({"category": "important", "priority": 1, "assessment": "r"})
    for invalid in (
        {"category": "other", "priority": 1, "assessment": "r"},
        {"category": "important", "priority": 4, "assessment": "r"},
        {"category": "important", "priority": 2, "assessment": ""},
    ):
        with pytest.raises(ValidationError):
            schema.model_validate(invalid)


async def test_classify_returns_the_chosen_category(fake_llm: FakeLLM) -> None:
    fake_llm.answer(answer("newsletter", 3, "  A subscribed\nnewsletter. "))

    decision = await classify(fake_llm.gateway, VIEW, CATEGORIES, language="en")

    assert decision.category is CATEGORIES[1]
    assert decision.priority == 3
    assert decision.reason == "A subscribed newsletter."
    (call,) = fake_llm.provider.calls
    assert call.schema is not None
    assert call.options is not None and call.options.temperature == 0.0


async def test_classify_retries_an_unknown_category(fake_llm: FakeLLM) -> None:
    fake_llm.answer(answer("invented"), answer("important", 1))

    decision = await classify(fake_llm.gateway, VIEW, CATEGORIES)

    assert decision.category is CATEGORIES[0]
    assert len(fake_llm.provider.calls) == 2


async def test_classify_gives_up_after_the_retries(fake_llm: FakeLLM) -> None:
    fake_llm.answer(*["not json"] * 3)

    with pytest.raises(LLMOutputError):
        await classify(fake_llm.gateway, VIEW, CATEGORIES)


async def test_classify_needs_categories(fake_llm: FakeLLM) -> None:
    with pytest.raises(ValueError, match="no categories"):
        await classify(fake_llm.gateway, VIEW, [])


def test_answer_helper_matches_schema() -> None:
    schema = decision_schema(["important"])
    assert schema.model_validate(json.loads(answer("important")))


def _builtin(key: str) -> EffectiveCategory:
    return EffectiveCategory(
        id=uuid.uuid4(),
        key=key,
        name=key.title(),
        description="",
        builtin_key=key,
        owner_user_id=None,
        position=0,
        hidden=False,
    )


def test_prompt_has_rules_only_for_visible_builtin_categories() -> None:
    categories = [_builtin("spam"), _builtin("waiting_for"), _category("project_x", "Project X.")]

    system, _ = build_messages(VIEW, categories, [], "en")

    assert "Check in this order and choose the first category that fits:" in system.content
    assert "1. spam: unsolicited offers" in system.content
    assert "2. waiting_for: no request" in system.content
    assert "action_required:" not in system.content.split("Check in this order")[-1]
    assert "- project_x:" not in system.content.split("Check in this order")[1]


def test_prompt_without_builtin_categories_has_no_rules() -> None:
    system, _ = build_messages(VIEW, CATEGORIES, [], "de")

    assert "Prüfe in dieser Reihenfolge" not in system.content


def test_decision_schema_asks_for_the_reason_first() -> None:
    # Ollama orders the grammar by property name, so the name has to sort first, too.
    schema = decision_schema(["important"])

    assert list(schema.model_json_schema()["properties"]) == ["assessment", "category", "priority"]


def test_builtin_examples_use_the_category_keys() -> None:
    categories = [_builtin("spam"), _builtin("info")]

    system, _ = build_messages(VIEW, categories, [], "de")

    examples = system.content.split("Beispiele:\n")[1]
    assert "Passwort ein. → category=spam" in examples
    assert "das Parkhaus wird gereinigt. → category=info" in examples
    assert "category=waiting_for" not in examples
