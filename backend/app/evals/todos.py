"""Todo stage: the real extraction prompt and post-processing for every mail.

Uses ``build_prompt`` and ``plan_extraction`` of ``app.todos.extraction`` (confidence
filter, deterministic due dates) on transient ORM objects; nothing touches a database.
As in the processing step, mails whose category is in ``OLLAMAIL_TODOS_SKIP_CATEGORIES``
are not sent to the model. The expected category decides this, so a triage mistake does
not distort the todo numbers; expected todos of skipped mails are reported separately.
"""

import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.ai.llm import LLMError, LLMGateway, LLMTask
from app.ai.prompts.todos import TODOS_EXTRACT
from app.core.config import TodosSettings
from app.evals.dataset import OWNER_ADDRESS, OWNER_NAME, OWNER_TIMEZONE, EvalMail
from app.evals.metrics import ExpectedTask, PredictedTodo, TodoScore, score_todos
from app.evals.triage import recipients
from app.mail.models import Mailbox, MailboxType, Message
from app.todos.extraction import (
    TodoExtraction,
    build_prompt,
    is_outgoing,
    plan_extraction,
    reference_date,
)
from app.users.models import User


def transient_mail(mail: EvalMail) -> tuple[Message, Mailbox, User]:
    """ORM objects for one mail, never added to a session."""
    mailbox = Mailbox(
        id=uuid.uuid4(),
        type=MailboxType.IMAP,
        display_name="Eval",
        address=OWNER_ADDRESS,
        owner_user_id=uuid.uuid4(),
    )
    user = User(
        id=mailbox.owner_user_id,
        email=OWNER_ADDRESS,
        display_name=OWNER_NAME,
        timezone=OWNER_TIMEZONE,
    )
    to, cc = recipients(mail)
    message = Message(
        id=uuid.uuid4(),
        mailbox_id=mailbox.id,
        remote_ref=mail.id,
        subject=mail.subject,
        sender=mail.sender.model_dump(),
        to=to,
        cc=cc,
        sent_at=mail.sent_at,
        body_text=mail.body,
        body_main=mail.body,
        language=mail.language,
    )
    return message, mailbox, user


@dataclass(frozen=True)
class TodoOutcome:
    mail_id: str
    expected: list[ExpectedTask]
    predicted: list[PredictedTodo]
    score: TodoScore
    seconds: float
    error: str | None = None


@dataclass
class TodosReport:
    outcomes: list[TodoOutcome] = field(default_factory=list)
    # Mails not sent to the model because of their category, and their expected todos.
    skipped_mails: int = 0
    skipped_expected: int = 0

    @property
    def total(self) -> TodoScore:
        total = TodoScore()
        for outcome in self.outcomes:
            total += outcome.score
        return total

    def as_dict(self) -> dict[str, object]:
        total = self.total
        return {
            "mails": len(self.outcomes),
            "expected_todos": total.true_positives + total.false_negatives,
            "predicted_todos": total.true_positives + total.false_positives,
            "precision": round(total.precision, 4),
            "recall": round(total.recall, 4),
            "f1": round(total.f1, 4),
            "due_date_accuracy": round(total.due_accuracy, 4),
            "due_dates_checked": total.due_checked,
            "errors": sum(o.error is not None for o in self.outcomes),
            "skipped_mails": self.skipped_mails,
            "skipped_expected_todos": self.skipped_expected,
            "mistakes": [
                {
                    "mail": o.mail_id,
                    "missed": o.score.false_negatives,
                    "extra": o.score.false_positives,
                    "wrong_due": o.score.due_checked - o.score.due_correct,
                    **({"error": o.error} if o.error else {}),
                }
                for o in self.outcomes
                if o.error
                or o.score.false_negatives
                or o.score.false_positives
                or o.score.due_checked != o.score.due_correct
            ],
        }


def expected_tasks(mail: EvalMail) -> list[ExpectedTask]:
    return [ExpectedTask(t.title, tuple(t.keywords), t.due_date) for t in mail.todos]


async def run_todos(
    llm: LLMGateway, mails: Sequence[EvalMail], *, settings: TodosSettings | None = None
) -> TodosReport:
    settings = settings or TodosSettings()
    skip = {key.lower() for key in settings.skip_categories}
    report = TodosReport()
    for mail in mails:
        expected = expected_tasks(mail)
        if mail.category in skip:
            report.skipped_mails += 1
            report.skipped_expected += len(expected)
            continue
        message, mailbox, user = transient_mail(mail)
        reference = reference_date(message, OWNER_TIMEZONE)
        started = time.perf_counter()
        try:
            result = await llm.complete_structured(
                LLMTask.TODOS,
                build_prompt(message, mailbox, user, reference, []),
                TodoExtraction,
                prompt_version=TODOS_EXTRACT.id,
                language=mail.language,
            )
        except LLMError as exc:
            report.outcomes.append(
                TodoOutcome(
                    mail.id,
                    expected,
                    [],
                    score_todos(expected, []),
                    time.perf_counter() - started,
                    error=type(exc).__name__,
                )
            )
            continue
        plan = plan_extraction(
            result,
            open_titles=[],
            reference=reference,
            min_confidence=settings.min_confidence,
            outgoing=is_outgoing(message, mailbox),
        )
        predicted = [PredictedTodo(p.item.title, p.item.description, p.due) for p in plan.todos]
        report.outcomes.append(
            TodoOutcome(
                mail.id,
                expected,
                predicted,
                score_todos(expected, predicted),
                time.perf_counter() - started,
            )
        )
    return report
