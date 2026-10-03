"""Evaluate the todo extraction against labelled synthetic mails.

Runs the real prompt, the model and the post-processing (confidence filter, due-date
resolution, thread matching) on every case of ``eval_cases.json`` and prints precision,
recall and due-date accuracy per model. Nothing is written to the database.

    python -m app.todos.evaluation                      # model of the ``todos`` task
    python -m app.todos.evaluation --model qwen2.5:3b --model llama3.2:3b

All mails in the data set are invented (docs/PRIVACY.md); add cases in the same format.
"""

import argparse
import asyncio
import json
import re
import sys
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.ai.llm import EnvConfigResolver, LLMGateway
from app.ai.llm.types import LLMTask
from app.ai.prompts.todos import TODOS_EXTRACT
from app.core.config import TodosSettings, get_settings
from app.mail.models import Mailbox, MailboxType, Message
from app.todos.extraction import (
    ExtractionPlan,
    TodoExtraction,
    build_prompt,
    is_outgoing,
    plan_extraction,
    reference_date,
)
from app.todos.models import Todo
from app.users.models import User

CASES_FILE = Path(__file__).with_name("eval_cases.json")


class Address(BaseModel):
    name: str | None = None
    address: str


class ExpectedTodo(BaseModel):
    # Matches if any keyword occurs in title or description (case-insensitive).
    keywords: list[str] = Field(min_length=1)
    due_date: date | None = None
    # The deadline as written in the mail; checked against ``due_date`` in the tests.
    due_phrase: str | None = None


class ExpectedUpdate(BaseModel):
    # Number of the open todo (1-based, as shown to the model).
    todo: int = Field(ge=1)
    due_date: date | None = None
    due_phrase: str | None = None


class Expected(BaseModel):
    todos: list[ExpectedTodo] = Field(default_factory=list)
    updates: list[ExpectedUpdate] = Field(default_factory=list)
    done: list[int] = Field(default_factory=list)


class OpenTodo(BaseModel):
    title: str
    due_date: date | None = None


class EvalCase(BaseModel):
    id: str
    language: str
    timezone: str = "Europe/Berlin"
    sent_at: datetime
    user: Address
    sender: Address
    to: list[Address]
    subject: str
    body: str
    open_todos: list[OpenTodo] = Field(default_factory=list)
    expected: Expected


def load_cases(path: Path = CASES_FILE) -> list[EvalCase]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [EvalCase.model_validate(item) for item in data]


@dataclass(frozen=True)
class CaseInput:
    message: Message
    mailbox: Mailbox
    user: User
    open_todos: list[Todo]
    reference: date


def case_input(case: EvalCase) -> CaseInput:
    """Transient ORM objects (never added to a session) for one case."""
    mailbox = Mailbox(
        id=uuid.uuid4(),
        type=MailboxType.IMAP,
        display_name="Eval",
        address=case.user.address,
        owner_user_id=uuid.uuid4(),
    )
    user = User(
        id=mailbox.owner_user_id,
        email=case.user.address,
        display_name=case.user.name or case.user.address,
        timezone=case.timezone,
    )
    message = Message(
        id=uuid.uuid4(),
        mailbox_id=mailbox.id,
        remote_ref=case.id,
        subject=case.subject,
        sender=case.sender.model_dump(),
        to=[a.model_dump() for a in case.to],
        cc=[],
        sent_at=case.sent_at,
        body_text=case.body,
        body_main=case.body,
        language=case.language,
    )
    open_todos = [Todo(title=t.title, due_date=t.due_date) for t in case.open_todos]
    return CaseInput(message, mailbox, user, open_todos, reference_date(message, case.timezone))


def _text(item: Any) -> str:
    return f"{item.title} {item.description or ''}".casefold()


def _matches(expected: ExpectedTodo, text: str) -> bool:
    return any(re.search(re.escape(k.casefold()), text) for k in expected.keywords)


@dataclass
class CaseScore:
    case_id: str
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    due_checked: int = 0
    due_correct: int = 0
    updates_correct: bool = True
    done_correct: bool = True
    error: str | None = None
    seconds: float = 0.0

    @property
    def passed(self) -> bool:
        return (
            self.error is None
            and self.false_positives == 0
            and self.false_negatives == 0
            and self.due_checked == self.due_correct
            and self.updates_correct
            and self.done_correct
        )


def score_case(case: EvalCase, plan: ExtractionPlan) -> CaseScore:
    score = CaseScore(case.id)
    new = [p for p in plan.todos if p.updates is None]
    unmatched = list(new)
    for expected in case.expected.todos:
        hit = next((p for p in unmatched if _matches(expected, _text(p.item))), None)
        if hit is None:
            score.false_negatives += 1
            continue
        unmatched.remove(hit)
        score.true_positives += 1
        score.due_checked += 1
        score.due_correct += hit.due == expected.due_date
    score.false_positives = len(unmatched)

    updates = {p.updates: p.due for p in plan.todos if p.updates is not None}
    wanted = {u.todo - 1: u.due_date for u in case.expected.updates}
    score.updates_correct = set(updates) == set(wanted) and all(
        wanted[i] is None or updates[i] == wanted[i] for i in wanted
    )
    score.done_correct = set(plan.done) == {n - 1 for n in case.expected.done}
    return score


async def run_case(
    llm: LLMGateway, case: EvalCase, settings: TodosSettings
) -> tuple[CaseScore, ExtractionPlan | None]:
    data = case_input(case)
    started = time.perf_counter()
    try:
        result = await llm.complete_structured(
            LLMTask.TODOS,
            build_prompt(data.message, data.mailbox, data.user, data.reference, data.open_todos),
            TodoExtraction,
            prompt_version=TODOS_EXTRACT.id,
            language=case.language,
        )
    except Exception as exc:
        score = CaseScore(case.id, error=type(exc).__name__)
        score.false_negatives = len(case.expected.todos)
        return score, None
    plan = plan_extraction(
        result,
        open_titles=[t.title for t in data.open_todos],
        reference=data.reference,
        min_confidence=settings.min_confidence,
        outgoing=is_outgoing(data.message, data.mailbox),
        max_todos=settings.max_per_mail,
    )
    score = score_case(case, plan)
    score.seconds = time.perf_counter() - started
    return score, plan


@dataclass
class Report:
    model: str
    scores: list[CaseScore] = field(default_factory=list)

    def _sum(self, name: str) -> int:
        return sum(int(getattr(s, name)) for s in self.scores)

    @property
    def precision(self) -> float:
        tp, fp = self._sum("true_positives"), self._sum("false_positives")
        return tp / (tp + fp) if tp + fp else 1.0

    @property
    def recall(self) -> float:
        tp, fn = self._sum("true_positives"), self._sum("false_negatives")
        return tp / (tp + fn) if tp + fn else 1.0

    @property
    def due_accuracy(self) -> float:
        checked = self._sum("due_checked")
        return self._sum("due_correct") / checked if checked else 1.0

    @property
    def passed(self) -> int:
        return sum(s.passed for s in self.scores)

    def render(self) -> str:
        lines = [f"Model: {self.model}"]
        for s in self.scores:
            status = "ok  " if s.passed else ("ERR " if s.error else "FAIL")
            detail = f" ({s.error})" if s.error else ""
            lines.append(
                f"  {status} {s.case_id:<32} tp={s.true_positives} fp={s.false_positives} "
                f"fn={s.false_negatives} due={s.due_correct}/{s.due_checked} "
                f"{s.seconds:5.1f}s{detail}"
            )
        lines.append(
            f"  cases {self.passed}/{len(self.scores)}  precision {self.precision:.2f}  "
            f"recall {self.recall:.2f}  due dates {self.due_accuracy:.2f}"
        )
        return "\n".join(lines)


async def evaluate(
    llm: LLMGateway, cases: Sequence[EvalCase], model: str, settings: TodosSettings
) -> Report:
    report = Report(model)
    for case in cases:
        score, _ = await run_case(llm, case, settings)
        report.scores.append(score)
    return report


async def _main(models: Sequence[str | None], cases: list[EvalCase]) -> list[Report]:
    settings = get_settings()
    reports = []
    for model in models:
        llm_settings = settings.llm
        if model:
            llm_settings = llm_settings.model_copy(update={"task_todos_model": model})
        resolver = EnvConfigResolver(llm_settings)
        llm = LLMGateway(resolver)
        assigned = (await resolver.resolve(LLMTask.TODOS)).model
        try:
            reports.append(await evaluate(llm, cases, assigned, settings.todos))
        finally:
            await llm.aclose()
    return reports


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--model", action="append", help="Model to test; repeat for several.")
    parser.add_argument("--cases", type=Path, default=CASES_FILE, help="Data set (JSON).")
    args = parser.parse_args(argv)
    reports = asyncio.run(_main(args.model or [None], load_cases(args.cases)))
    for report in reports:
        print(report.render())
    return 0


if __name__ == "__main__":
    sys.exit(main())
