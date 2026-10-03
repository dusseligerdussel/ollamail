"""Triage stage: the rule-based pre-filter and the real classification prompt.

Every mail is classified as the processing step would do it for a new user: built-in
default categories, no sender rules and no few-shot examples. Mails the pre-filter
decides never reach the model, as in production; ``use_prefilter=False`` sends all of
them to the model.
"""

import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.ai.llm import LLMError
from app.core.config import TriageSettings
from app.evals.dataset import CATEGORIES, OWNER_ADDRESS, EvalMail
from app.evals.metrics import Confusion
from app.triage.categories import DEFAULT_CATEGORIES, EffectiveCategory
from app.triage.classify import StructuredLLM, classify, mail_view
from app.triage.rules import prefilter

# Recipients of a mail that was sent to a list (the owner is neither in To nor in Cc).
LIST_ADDRESS = "list@example.org"


def default_categories() -> list[EffectiveCategory]:
    """The organisation defaults as the migration creates them, all visible."""
    return [
        EffectiveCategory(
            id=uuid.uuid5(uuid.NAMESPACE_URL, f"ollamail:triage:{d.key}"),
            key=d.key,
            name=d.name,
            description=d.description,
            builtin_key=d.key,
            owner_user_id=None,
            position=position,
            hidden=False,
        )
        for position, d in enumerate(DEFAULT_CATEGORIES)
    ]


def recipients(mail: EvalMail) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """``(to, cc)`` of a mail as stored after the sync."""
    owner = [{"name": "Robin Beispiel", "address": OWNER_ADDRESS}]
    listed = [{"address": LIST_ADDRESS}]
    if mail.addressed == "cc":
        return listed, owner
    if mail.addressed == "other":
        return listed, []
    return owner, []


@dataclass(frozen=True)
class TriageOutcome:
    mail_id: str
    expected: str
    predicted: str
    expected_priority: int
    priority: int | None
    by_rule: bool
    seconds: float
    error: str | None = None


@dataclass
class TriageReport:
    outcomes: list[TriageOutcome] = field(default_factory=list)

    @property
    def confusion(self) -> Confusion:
        confusion = Confusion()
        for o in self.outcomes:
            confusion.add(o.expected, o.predicted)
        return confusion

    def as_dict(self) -> dict[str, object]:
        confusion = self.confusion
        by_rule = [o for o in self.outcomes if o.by_rule]
        by_model = [o for o in self.outcomes if not o.by_rule]
        return {
            "mails": len(self.outcomes),
            "accuracy": round(confusion.accuracy, 4),
            "accuracy_model_only": round(
                sum(o.expected == o.predicted for o in by_model) / len(by_model), 4
            )
            if by_model
            else None,
            "priority_accuracy": round(
                sum(o.expected_priority == o.priority for o in self.outcomes) / len(self.outcomes),
                4,
            )
            if self.outcomes
            else 0.0,
            "decided_by_rule": len(by_rule),
            "rule_correct": sum(o.expected == o.predicted for o in by_rule),
            "errors": sum(o.error is not None for o in self.outcomes),
            "recall_per_category": {
                k: round(v, 4) for k, v in confusion.per_class_recall(CATEGORIES).items()
            },
            "confusion": confusion.matrix(CATEGORIES),
            "misclassified": [
                {"mail": o.mail_id, "expected": o.expected, "predicted": o.predicted}
                for o in self.outcomes
                if o.expected != o.predicted
            ],
        }


async def run_triage(
    llm: StructuredLLM,
    mails: Sequence[EvalMail],
    *,
    use_prefilter: bool = True,
    settings: TriageSettings | None = None,
) -> TriageReport:
    settings = settings or TriageSettings()
    categories = default_categories()
    key_of = {c.id: c.key for c in categories}
    report = TriageReport()
    for mail in mails:
        started = time.perf_counter()
        ruled = (
            prefilter(mail.headers, mail.sender.address, [], categories) if use_prefilter else None
        )
        if ruled is not None:
            report.outcomes.append(
                TriageOutcome(
                    mail.id,
                    mail.category,
                    key_of[ruled.category_id],
                    mail.priority,
                    ruled.priority,
                    by_rule=True,
                    seconds=time.perf_counter() - started,
                )
            )
            continue
        to, cc = recipients(mail)
        view = mail_view(
            subject=mail.subject,
            sender=mail.sender.model_dump(),
            to=to,
            cc=cc,
            mailbox_address=OWNER_ADDRESS,
            date=mail.sent_at,
            body_main=mail.body,
            body_text=mail.body,
            body_chars=settings.max_body_chars,
        )
        try:
            decision = await classify(llm, view, categories, language=mail.language)
        except LLMError as exc:
            report.outcomes.append(
                TriageOutcome(
                    mail.id,
                    mail.category,
                    "error",
                    mail.priority,
                    None,
                    by_rule=False,
                    seconds=time.perf_counter() - started,
                    error=type(exc).__name__,
                )
            )
            continue
        report.outcomes.append(
            TriageOutcome(
                mail.id,
                mail.category,
                decision.category.key,
                mail.priority,
                decision.priority,
                by_rule=False,
                seconds=time.perf_counter() - started,
            )
        )
    return report
