"""LLM classification with structured output: category, priority (1-3) and a one-sentence
reason. Pure prompt building plus one gateway call; no database access.

The answer schema is built per request with the allowed category keys as ``enum``, so
endpoints with native structured output can only answer with a valid key.

Prompt injection (#170): the mail goes into a data block with a random tag, and passages
addressed to an AI assistant are removed before the call (``app.ai.injection``). A mail
that contained such passages never gets priority 1, and its reason tells the user to
check the category, so the instructions cannot lift it to the top of the inbox.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Annotated, Any, Literal, Protocol

from pydantic import AfterValidator, BaseModel, Field, create_model

from app.ai import injection
from app.ai.llm import ChatMessage, GenerationOptions, LLMTask
from app.triage.categories import EffectiveCategory
from app.triage.prompts import (
    BUILTIN_EXAMPLES,
    BUILTIN_EXAMPLES_HEADING,
    BUILTIN_RULES,
    EXAMPLES_HEADING,
    REVIEW_REASON,
    RULES_HEADING,
    TRIAGE_PROMPT,
)

REASON_MAX_LENGTH = 300
_WHITESPACE = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES = re.compile(r"\n{3,}")

Addressed = Literal["to", "cc", "other"]


class StructuredLLM(Protocol):
    """The part of ``LLMGateway`` the classification needs (tests pass a gateway with a
    fake provider)."""

    async def complete_structured[T: BaseModel](
        self,
        task: LLMTask,
        messages: Sequence[ChatMessage],
        schema: type[T],
        *,
        prompt_version: str | None = None,
        options: GenerationOptions | None = None,
        language: str | None = None,
    ) -> T: ...


@dataclass(frozen=True, slots=True)
class MailView:
    """What the model gets to see of one mail."""

    subject: str
    sender_name: str | None
    sender_address: str | None
    addressed: Addressed
    date: datetime | None
    body: str


@dataclass(frozen=True, slots=True)
class Example:
    """One correction of the user, rendered into the prompt."""

    mail: MailView
    category_key: str
    priority: int


@dataclass(frozen=True, slots=True)
class LLMDecision:
    category: EffectiveCategory
    priority: int
    reason: str
    # Passages addressed to an AI assistant that were removed before the call.
    injected_passages: int = 0


def clean_text(text: str, limit: int) -> str:
    """Whitespace-normalised text, cut at ``limit`` characters."""
    lines = (_WHITESPACE.sub(" ", line).strip() for line in text.splitlines())
    compact = _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()
    if len(compact) <= limit:
        return compact
    return compact[:limit].rstrip() + " …"


def _addresses(values: Sequence[dict[str, Any]] | None) -> set[str]:
    return {str(v.get("address", "")).lower() for v in values or () if isinstance(v, dict)}


def mail_view(
    *,
    subject: str,
    sender: dict[str, Any] | None,
    to: Sequence[dict[str, Any]] | None,
    cc: Sequence[dict[str, Any]] | None,
    mailbox_address: str,
    date: datetime | None,
    body_main: str,
    body_text: str,
    body_chars: int,
) -> MailView:
    own = mailbox_address.strip().lower()
    addressed: Addressed = "other"
    if own in _addresses(to):
        addressed = "to"
    elif own in _addresses(cc):
        addressed = "cc"
    return MailView(
        subject=clean_text(subject, 300),
        sender_name=(sender or {}).get("name") or None,
        sender_address=(sender or {}).get("address") or None,
        addressed=addressed,
        date=date,
        body=clean_text(body_main or body_text, body_chars),
    )


_ADDRESSED_TEXT = {
    "to": "directly (To)",
    "cc": "in copy (Cc)",
    "other": "not listed (mailing list, Bcc or forward)",
}


def render_mail(view: MailView) -> str:
    sender = view.sender_address or "unknown"
    if view.sender_name:
        sender = f"{view.sender_name} <{sender}>"
    lines = [
        f"From: {sender}",
        f"Recipient addressed: {_ADDRESSED_TEXT[view.addressed]}",
        f"Subject: {view.subject}",
    ]
    if view.date is not None:
        lines.append(f"Date: {view.date:%Y-%m-%d %H:%M %Z}".rstrip())
    lines += ["", view.body or "(no text)"]
    return "\n".join(lines)


def protect(view: MailView) -> tuple[MailView, int]:
    """``view`` without passages addressed to an AI assistant, and how many there were."""
    subject = injection.neutralize(view.subject)
    body = injection.neutralize(view.body)
    passages = subject.passages + body.passages
    if not passages:
        return view, 0
    return replace(view, subject=subject.text, body=body.text), passages


def _render_example(example: Example) -> str:
    mail, _ = protect(example.mail)
    excerpt = mail.body.replace("\n", " ")
    return (
        f"- From: {mail.sender_address or 'unknown'} | Subject: {mail.subject} | "
        f"Text: {excerpt} → category={example.category_key}, priority={example.priority}\n"
    )


def build_messages(
    view: MailView,
    categories: Sequence[EffectiveCategory],
    examples: Sequence[Example],
    language: str | None,
    *,
    tag: str | None = None,
) -> list[ChatMessage]:
    """Prompt for ``view``; the mail goes into a data block named ``tag`` (random per
    call unless given)."""
    lang = TRIAGE_PROMPT.language_for(language)
    tag = tag or injection.data_tag()
    category_lines = "\n".join(
        f"- {c.key}: {c.name}" + (f". {clean_text(c.description, 500)}" if c.description else "")
        for c in categories
    )
    examples_text = ""
    if examples:
        examples_text = EXAMPLES_HEADING[lang] + "".join(_render_example(e) for e in examples)
    examples_text += "\n"
    return TRIAGE_PROMPT.render(
        lang,
        categories=category_lines,
        rules=_render_rules(categories, lang),
        examples=examples_text,
        tag=tag,
        mail=injection.data_block(tag, render_mail(view)),
    )


def _render_rules(categories: Sequence[EffectiveCategory], lang: str) -> str:
    """Numbered decision rules and one synthetic example for each visible built-in
    category, in check order."""
    by_builtin = {c.builtin_key: c for c in categories if c.builtin_key}
    rules = [(b, rule) for b, rule in BUILTIN_RULES[lang].items() if b in by_builtin]
    if not rules:
        return ""
    lines = [
        f"{number}. {by_builtin[builtin].key}: {rule}\n"
        for number, (builtin, rule) in enumerate(rules, start=1)
    ]
    examples = [
        f"- {text} → category={by_builtin[builtin].key}\n"
        for builtin, text in BUILTIN_EXAMPLES[lang].items()
        if builtin in by_builtin
    ]
    return RULES_HEADING[lang] + "".join(lines) + BUILTIN_EXAMPLES_HEADING[lang] + "".join(examples)


def decision_schema(keys: Sequence[str]) -> type[BaseModel]:
    """``{"assessment": str, "category": <one of keys>, "priority": 1..3}``.

    The one-sentence reason comes first, so the category follows from it instead of
    being justified afterwards. Models write fields in schema order, and Ollama's
    grammar sorts them by name, hence ``assessment`` rather than ``reason``.
    """
    allowed = list(keys)

    def known(value: str) -> str:
        if value not in allowed:
            raise ValueError("unknown category")
        return value

    category = Annotated[str, Field(json_schema_extra={"enum": allowed}), AfterValidator(known)]
    model: type[BaseModel] = create_model(
        "TriageDecision",
        assessment=(str, Field(min_length=1, max_length=REASON_MAX_LENGTH)),
        category=(category, ...),
        priority=(int, Field(ge=1, le=3)),
    )
    return model


async def classify(
    llm: StructuredLLM,
    view: MailView,
    categories: Sequence[EffectiveCategory],
    examples: Sequence[Example] = (),
    *,
    language: str | None = None,
) -> LLMDecision:
    """Ask the triage model; raises ``LLMError`` subclasses of the gateway."""
    if not categories:
        raise ValueError("no categories to choose from")
    by_key = {category.key: category for category in categories}
    schema = decision_schema(list(by_key))
    view, injected = protect(view)
    injection.count("triage", injected)
    messages = build_messages(view, categories, examples, language)
    answer = await llm.complete_structured(
        LLMTask.TRIAGE,
        messages,
        schema,
        prompt_version=TRIAGE_PROMPT.id,
        options=GenerationOptions(temperature=0.0, max_tokens=200),
        language=TRIAGE_PROMPT.language_for(language),
    )
    values = answer.model_dump()
    reason = clean_text(str(values["assessment"]), REASON_MAX_LENGTH).replace("\n", " ")
    priority = int(values["priority"])
    if injected:
        # Plausibility: a mail that tries to steer the assistant is never urgent, and
        # the user is asked to check where it landed.
        priority = max(priority, 2)
        reason = REVIEW_REASON[TRIAGE_PROMPT.language_for(language)]
    return LLMDecision(by_key[values["category"]], priority, reason, injected)
