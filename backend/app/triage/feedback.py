"""Corrections as few-shot examples, and sender rules suggested from them.

Isolation (docs/PRIVACY.md, Zweckbindung): every query here filters on the user *and* on
mailboxes owned by that user. Examples of one user never reach another user's prompt;
``tests/triage/test_isolation.py`` checks this. Shared mailboxes (#34) are the exception
by design: corrections there apply to the whole mailbox, so its examples are the
corrections of all its users, taken only from mails of that same mailbox.
"""

import math
import uuid
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.llm import LLMError, LLMTask
from app.core.config import TriageSettings
from app.core.logging import get_logger
from app.mail.models import Mailbox, Message
from app.triage.categories import EffectiveCategory
from app.triage.classify import Example, MailView, mail_view
from app.triage.models import TriageFeedback, TriageSenderRule
from app.triage.rules import normalize_sender

log = get_logger(__name__)


class EmbeddingLLM(Protocol):
    async def embed(
        self, texts: Sequence[str], *, task: LLMTask = LLMTask.EMBEDDINGS
    ) -> list[list[float]]: ...

    async def assigned_model(self, task: LLMTask) -> str: ...


@dataclass(slots=True)
class _Candidate:
    feedback: TriageFeedback
    view: MailView
    key: str


def embedding_text(view: MailView) -> str:
    return f"{view.sender_address or ''}\n{view.subject}\n{view.body}"


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b):
        return -1.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else -1.0


async def _candidates(
    session: AsyncSession,
    user_id: uuid.UUID | None,
    categories: Sequence[EffectiveCategory],
    exclude_message_id: uuid.UUID | None,
    settings: TriageSettings,
    shared_mailbox_id: uuid.UUID | None,
) -> list[_Candidate]:
    keys = {category.id: category.key for category in categories}
    if shared_mailbox_id is not None:
        scope = and_(Mailbox.id == shared_mailbox_id, Mailbox.is_shared)
    elif user_id is not None:
        scope = and_(TriageFeedback.user_id == user_id, Mailbox.owner_user_id == user_id)
    else:
        return []
    statement = (
        select(TriageFeedback, Message, Mailbox.address)
        .join(Message, Message.id == TriageFeedback.message_id)
        .join(Mailbox, Mailbox.id == Message.mailbox_id)
        .where(scope, TriageFeedback.category_id.in_(list(keys)))
        .order_by(TriageFeedback.updated_at.desc(), TriageFeedback.id.desc())
        .limit(settings.few_shot_pool)
    )
    if exclude_message_id is not None:
        statement = statement.where(TriageFeedback.message_id != exclude_message_id)
    candidates = []
    for feedback, message, address in await session.execute(statement):
        view = mail_view(
            subject=message.subject,
            sender=message.sender,
            to=message.to,
            cc=message.cc,
            mailbox_address=address,
            date=message.received_at or message.sent_at,
            body_main=message.body_main,
            body_text=message.body_text,
            body_chars=settings.few_shot_body_chars,
        )
        candidates.append(_Candidate(feedback, view, keys[feedback.category_id]))
    return candidates


async def _rank_by_similarity(
    candidates: list[_Candidate], query: MailView, llm: EmbeddingLLM
) -> list[_Candidate]:
    """Candidates, most similar first. Stores missing embeddings on the feedback rows
    (written with the step's transaction)."""
    model = await llm.assigned_model(LLMTask.EMBEDDINGS)
    missing = [
        c for c in candidates if c.feedback.embedding is None or c.feedback.embedding_model != model
    ]
    if missing:
        vectors = await llm.embed([embedding_text(c.view) for c in missing])
        for candidate, vector in zip(missing, vectors, strict=True):
            candidate.feedback.embedding = list(vector)
            candidate.feedback.embedding_model = model
    (query_vector,) = await llm.embed([embedding_text(query)])

    def score(candidate: _Candidate) -> float:
        return cosine(candidate.feedback.embedding or [], query_vector)

    return sorted(candidates, key=score, reverse=True)


async def select_examples(
    session: AsyncSession,
    user_id: uuid.UUID | None,
    query: MailView,
    categories: Sequence[EffectiveCategory],
    settings: TriageSettings,
    *,
    llm: EmbeddingLLM | None = None,
    exclude_message_id: uuid.UUID | None = None,
    shared_mailbox_id: uuid.UUID | None = None,
) -> list[Example]:
    """Up to ``few_shot_examples`` corrections of ``user_id`` (or, with
    ``shared_mailbox_id``, of the shared mailbox) for the prompt: the most similar ones if
    embeddings are available, otherwise the most recent ones."""
    limit = settings.few_shot_examples
    if limit == 0:
        return []
    candidates = await _candidates(
        session, user_id, categories, exclude_message_id, settings, shared_mailbox_id
    )
    if len(candidates) > limit and settings.few_shot_embeddings and llm is not None:
        try:
            candidates = await _rank_by_similarity(candidates, query, llm)
        except LLMError as exc:
            # No embedding model: fall back to the most recent corrections.
            log.warning("triage_examples_embedding_failed", error_type=type(exc).__name__)
    return [Example(c.view, c.key, c.feedback.priority) for c in candidates[:limit]]


@dataclass(frozen=True, slots=True)
class RuleSuggestion:
    sender: str
    category_id: uuid.UUID
    priority: int
    corrections: int


async def suggest_sender_rules(
    session: AsyncSession, user_id: uuid.UUID, *, min_corrections: int, limit: int = 1000
) -> list[RuleSuggestion]:
    """Senders the user corrected at least ``min_corrections`` times, always to the same
    category, and without a sender rule yet."""
    rows = await session.execute(
        select(Message.sender, TriageFeedback.category_id, TriageFeedback.priority)
        .join(Message, Message.id == TriageFeedback.message_id)
        .join(Mailbox, Mailbox.id == Message.mailbox_id)
        .where(TriageFeedback.user_id == user_id, Mailbox.owner_user_id == user_id)
        .order_by(TriageFeedback.updated_at.desc())
        .limit(limit)
    )
    existing = set(
        await session.scalars(
            select(TriageSenderRule.sender).where(TriageSenderRule.user_id == user_id)
        )
    )
    by_sender: dict[str, list[tuple[uuid.UUID, int]]] = defaultdict(list)
    for sender, category_id, priority in rows:
        address = (sender or {}).get("address")
        if address:
            by_sender[normalize_sender(str(address))].append((category_id, priority))
    suggestions = []
    for sender, decisions in by_sender.items():
        categories = {category_id for category_id, _ in decisions}
        if sender in existing or len(decisions) < min_corrections or len(categories) != 1:
            continue
        priority = Counter(priority for _, priority in decisions).most_common(1)[0][0]
        suggestions.append(RuleSuggestion(sender, categories.pop(), priority, len(decisions)))
    suggestions.sort(key=lambda s: (-s.corrections, s.sender))
    return suggestions
