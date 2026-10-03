"""Digest stage: the real map-reduce summary for each day of the data set.

The mails of one day (in one language) form one digest, selected and ordered as
``app.digest.content`` does it: bulk categories are only counted, skipped categories are
left out, the rest is sorted by ``MailItem.rank``. The expected categories stand in for
the triage, so the numbers measure the summary alone.

A summary is plain text, so it is scored by rules: how many of the important mails
(``MailItem.is_important``) the summary references with ``[n]``, and whether it mentions
the deadlines of their expected todos (day of month or weekday as written in the mail).
"""

import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

from app.ai.llm import LLMError, LLMGateway
from app.core.config import DigestSettings
from app.digest.content import MailItem
from app.digest.models import DigestLength
from app.digest.summarize import Summarizer, references
from app.evals.dataset import OWNER_NAME, EvalMail, Language
from app.evals.metrics import normalize


def _sender(mail: EvalMail) -> str:
    return mail.sender.name or mail.sender.address


def digest_items(mails: Sequence[EvalMail], settings: DigestSettings) -> list[MailItem]:
    """The mails a digest would summarise, most important first."""
    bulk = {key.lower() for key in settings.bulk_categories}
    skipped = {key.lower() for key in settings.skip_categories}
    items = [
        MailItem(
            message_id=uuid.uuid5(uuid.NAMESPACE_URL, f"ollamail:eval:{mail.id}"),
            mailbox_id=uuid.UUID(int=0),
            sender=_sender(mail),
            subject=mail.subject,
            body=mail.body[: settings.map_body_chars],
            received_at=mail.sent_at,
            category=mail.category,
            priority=mail.priority,
        )
        for mail in mails
        if mail.category not in bulk | skipped
    ]
    items.sort(key=lambda item: item.rank)
    return items[: settings.max_messages]


def deadline_mentioned(summary: str, phrase: str) -> bool:
    """The summary repeats the deadline's key token (a number or a weekday/month word)."""
    text = normalize(summary)
    tokens = [t for t in normalize(phrase).split() if t.isdigit() or len(t) >= 4]
    return any(token in text.split() for token in tokens) if tokens else True


@dataclass(frozen=True)
class DigestOutcome:
    day: date
    language: str
    mails: int
    important: int
    important_referenced: int
    deadlines: int
    deadlines_mentioned: int
    words: int
    seconds: float
    error: str | None = None


@dataclass
class DigestReport:
    outcomes: list[DigestOutcome] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        important = sum(o.important for o in self.outcomes)
        deadlines = sum(o.deadlines for o in self.outcomes)
        return {
            "digests": len(self.outcomes),
            "mails": sum(o.mails for o in self.outcomes),
            "important_coverage": round(
                sum(o.important_referenced for o in self.outcomes) / important, 4
            )
            if important
            else None,
            "deadline_coverage": round(
                sum(o.deadlines_mentioned for o in self.outcomes) / deadlines, 4
            )
            if deadlines
            else None,
            "words_mean": round(sum(o.words for o in self.outcomes) / len(self.outcomes), 1)
            if self.outcomes
            else 0,
            "seconds_mean": round(sum(o.seconds for o in self.outcomes) / len(self.outcomes), 1)
            if self.outcomes
            else 0,
            "errors": sum(o.error is not None for o in self.outcomes),
            "days": [
                {
                    "day": o.day.isoformat(),
                    "language": o.language,
                    "important": f"{o.important_referenced}/{o.important}",
                    "deadlines": f"{o.deadlines_mentioned}/{o.deadlines}",
                    **({"error": o.error} if o.error else {}),
                }
                for o in self.outcomes
            ],
        }


def groups(mails: Sequence[EvalMail]) -> list[tuple[date, Language, list[EvalMail]]]:
    by_key: dict[tuple[date, Language], list[EvalMail]] = {}
    for mail in sorted(mails, key=lambda m: m.sent_at):
        by_key.setdefault((mail.day, mail.language), []).append(mail)
    return [(day, language, group) for (day, language), group in sorted(by_key.items())]


async def run_digest(
    llm: LLMGateway,
    mails: Sequence[EvalMail],
    *,
    settings: DigestSettings | None = None,
    length: DigestLength = DigestLength.NORMAL,
) -> DigestReport:
    settings = settings or DigestSettings()
    by_id = {uuid.uuid5(uuid.NAMESPACE_URL, f"ollamail:eval:{m.id}"): m for m in mails}
    report = DigestReport()
    for day, language, group in groups(mails):
        items = digest_items(group, settings)
        if not items:
            continue
        important = [ref for ref, item in enumerate(items, start=1) if item.is_important]
        deadlines = [
            (ref, todo.due_phrase)
            for ref, item in enumerate(items, start=1)
            if item.is_important
            for todo in by_id[item.message_id].todos
            if todo.due_phrase
        ]
        summarizer = Summarizer(
            llm, settings, language=language, length=length, user_label=OWNER_NAME
        )
        started = time.perf_counter()
        try:
            summary = await summarizer.summarize(items)
        except LLMError as exc:
            report.outcomes.append(
                DigestOutcome(
                    day,
                    language,
                    len(items),
                    len(important),
                    0,
                    len(deadlines),
                    0,
                    0,
                    time.perf_counter() - started,
                    error=type(exc).__name__,
                )
            )
            continue
        cited = set(references(summary.text))
        report.outcomes.append(
            DigestOutcome(
                day,
                language,
                len(items),
                len(important),
                sum(ref in cited for ref in important),
                len(deadlines),
                sum(deadline_mentioned(summary.text, phrase) for _, phrase in deadlines),
                len(summary.text.split()),
                time.perf_counter() - started,
            )
        )
    return report
