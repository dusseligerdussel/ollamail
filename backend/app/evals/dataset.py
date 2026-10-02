"""The evaluation data set: synthetic mails with expected results, and questions.

``data/mails.json`` holds the mails of one invented mailbox owner, each with its expected
triage category and priority and the todos a careful reader would note (title, keywords,
deadline as written and as date). ``data/questions.json`` holds questions for "ask your
inbox", with the mail(s) that answer them and keyword groups a correct answer contains;
some questions have no answer in any mail.

Everything is invented (docs/PRIVACY.md): names are made up, addresses use the
``example.com``/``example.org`` domains only. The tests check this.
"""

import json
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

DATA_DIR = Path(__file__).with_name("data")
MAILS_FILE = DATA_DIR / "mails.json"
QUESTIONS_FILE = DATA_DIR / "questions.json"

# The owner of the evaluated mailbox (invented).
OWNER_NAME = "Robin Beispiel"
OWNER_ADDRESS = "robin@example.org"
OWNER_TIMEZONE = "Europe/Berlin"

Language = Literal["de", "en"]
Addressed = Literal["to", "cc", "other"]
CATEGORIES = (
    "important",
    "action_required",
    "waiting_for",
    "info",
    "newsletter",
    "notification",
    "spam",
)
Category = Literal[
    "important", "action_required", "waiting_for", "info", "newsletter", "notification", "spam"
]


class Address(BaseModel):
    name: str | None = None
    address: str


class ExpectedTodo(BaseModel):
    title: str = Field(min_length=1)
    # Lower-case words or stems a correct title or description very likely contains.
    keywords: list[str] = Field(default_factory=list)
    # The deadline as written in the mail (verbatim) and the date it means.
    due_phrase: str | None = None
    due_date: date | None = None


class EvalMail(BaseModel):
    id: str
    language: Language
    sent_at: datetime
    sender: Address = Field(alias="from")
    addressed: Addressed = "to"
    headers: list[list[str]] = Field(default_factory=list)
    subject: str
    body: str
    category: Category
    priority: int = Field(ge=1, le=3)
    todos: list[ExpectedTodo] = Field(default_factory=list)

    model_config = {"populate_by_name": True}

    @property
    def day(self) -> date:
        return self.sent_at.date()


class Question(BaseModel):
    id: str
    language: Language
    question: str
    # Mails that contain the answer; empty for questions without an answer.
    sources: list[str] = Field(default_factory=list)
    # Keyword groups: a correct answer contains one alternative of every group.
    answer: list[list[str]] = Field(default_factory=list)
    no_answer: bool = False

    @model_validator(mode="after")
    def _consistent(self) -> "Question":
        if self.no_answer and (self.sources or self.answer):
            raise ValueError("a question without answer has no sources and no answer")
        if not self.no_answer and not (self.sources and self.answer):
            raise ValueError("an answerable question needs sources and answer keywords")
        return self


class Dataset(BaseModel):
    mails: list[EvalMail]
    questions: list[Question]

    def mail(self, mail_id: str) -> EvalMail:
        return next(m for m in self.mails if m.id == mail_id)

    def subset(self, *, languages: set[str] | None = None, limit: int | None = None) -> "Dataset":
        """Mails (and questions) of ``languages``; ``limit`` keeps every n-th mail so the
        sample keeps the mix of categories. Questions whose sources were dropped go too."""
        mails = [m for m in self.mails if languages is None or m.language in languages]
        if limit is not None and 0 < limit < len(mails):
            step = len(mails) / limit
            mails = [mails[int(i * step)] for i in range(limit)]
        kept = {m.id for m in mails}
        questions = [
            q
            for q in self.questions
            if (languages is None or q.language in languages)
            and all(source in kept for source in q.sources)
        ]
        return Dataset(mails=mails, questions=questions)

    def sample_questions(self, count: int) -> "Dataset":
        """Evenly spaced sample of ``count`` questions (keeps the mix of languages and of
        questions with and without answer); all mails stay, so retrieval is unchanged."""
        if not 0 < count < len(self.questions):
            return self
        step = len(self.questions) / count
        questions = [self.questions[int(i * step)] for i in range(count)]
        return Dataset(mails=self.mails, questions=questions)


def load_dataset(mails: Path = MAILS_FILE, questions: Path = QUESTIONS_FILE) -> Dataset:
    return Dataset(
        mails=json.loads(mails.read_text(encoding="utf-8")),
        questions=json.loads(questions.read_text(encoding="utf-8")),
    )
