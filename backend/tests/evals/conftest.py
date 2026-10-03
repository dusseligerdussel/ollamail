"""A fake "perfect" model for the evaluation: it answers every prompt of the features with
the expected result of the mail or question in the prompt, so the scores are known."""

import hashlib
import json
import math
import re
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field

import pytest
from pydantic import BaseModel

from app.ai.llm import ChatMessage, GenerationOptions, LLMResult, LLMUnavailableError
from app.ai.llm.config import EndpointConfig
from app.ai.llm.types import Usage
from app.digest.summarize import MapAnswer
from app.evals.dataset import Dataset, EvalMail, Question, load_dataset
from app.evals.rag import JudgeVerdict
from app.rag.query import QueryAnalysis
from app.rag.rerank import Ranking
from app.todos.extraction import TodoExtraction

DIMENSIONS = 256
_MAP_REF = re.compile(r"^\[(\d+)\] From: ", re.MULTILINE)
_NOTE_REF = re.compile(r"\[(\d+(?:, \d+)*)\]")
_WORD = re.compile(r"\w+")


def vector(text: str) -> list[float]:
    """Deterministic bag-of-words vector: texts sharing words are similar."""
    values = [0.0] * DIMENSIONS
    values[0] = 0.01
    for word in _WORD.findall(text.lower()):
        digest = hashlib.sha256(word.encode()).digest()
        values[int.from_bytes(digest[:2], "big") % DIMENSIONS] += 1.0
    norm = math.sqrt(sum(v * v for v in values))
    return [v / norm for v in values]


@dataclass
class Oracle:
    """Provider that knows the data set. ``broken`` models fail every call."""

    dataset: Dataset
    broken: set[str] = field(default_factory=set)
    calls: int = 0

    def _check(self, model: str) -> None:
        self.calls += 1
        if model in self.broken:
            raise LLMUnavailableError("down")

    def _mail(self, text: str) -> EvalMail:
        # Longest subject first, so a subject that contains another one wins.
        for mail in sorted(self.dataset.mails, key=lambda m: -len(m.subject)):
            if mail.subject in text:
                return mail
        raise AssertionError("prompt names no mail of the data set")

    def _question(self, text: str) -> Question:
        for question in self.dataset.questions:
            if question.question in text:
                return question
        raise AssertionError("prompt names no question of the data set")

    def _structured(self, schema: type[BaseModel], messages: Sequence[ChatMessage]) -> object:
        prompt = messages[-1].content
        if schema.__name__ == "TriageDecision":
            mail = self._mail(prompt)
            return {"assessment": "Fits.", "category": mail.category, "priority": mail.priority}
        if schema is TodoExtraction:
            mail = self._mail(prompt)
            return {
                "todos": [
                    {
                        "title": todo.title,
                        "due_phrase": todo.due_phrase,
                        "due_date": None,
                        "confidence": 0.9,
                    }
                    for todo in mail.todos
                ]
            }
        if schema is MapAnswer:
            items = [{"ref": int(ref), "summary": "Someone wants something."}
                     for ref in _MAP_REF.findall(prompt)]  # fmt: skip
            return {"items": items}
        if schema is QueryAnalysis:
            return {}
        if schema is Ranking:
            return {"ranking": []}
        if schema is JudgeVerdict:
            return {"correct": True, "reason": "ok"}
        raise AssertionError(f"unexpected schema {schema.__name__}")

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str,
        schema: type[BaseModel] | None = None,
        options: GenerationOptions | None = None,
    ) -> LLMResult:
        self._check(model)
        if schema is not None:
            content = json.dumps(self._structured(schema, messages))
        else:
            # Digest condense/reduce: one sentence citing every note.
            refs = sorted({int(n) for group in _NOTE_REF.findall(messages[-1].content)
                           for n in group.split(", ")})  # fmt: skip
            content = f"Here is the summary [{', '.join(map(str, refs))}]."
        return LLMResult(content=content, model=model, usage=Usage(100, 20))

    async def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str,
        options: GenerationOptions | None = None,
    ) -> AsyncIterator[str]:
        self._check(model)
        question = self._question(messages[-1].content)
        if question.no_answer:
            text = "I found nothing about this in the e-mails."
        else:
            text = " ".join(group[0] for group in question.answer) + " [1]"
        for start in range(0, len(text), 4):
            yield text[start : start + 4]

    async def embed(self, texts: Sequence[str], *, model: str) -> list[list[float]]:
        self._check(model)
        return [vector(text) for text in texts]

    async def list_models(self) -> list[str]:
        return []

    async def aclose(self) -> None:
        pass


@pytest.fixture(scope="session")
def dataset() -> Dataset:
    return load_dataset()


@pytest.fixture
def oracle(dataset: Dataset) -> Oracle:
    return Oracle(dataset)


@dataclass
class SteppingClock:
    """Monotonic nanosecond clock that advances by ``step_ns`` on every reading: the
    gateway reads it once before and once after a call, so every (sequential) call
    takes exactly ``step_ns`` regardless of how fast the test machine is."""

    step_ns: int = 50_000_000
    now_ns: int = 0

    def __call__(self) -> int:
        self.now_ns += self.step_ns
        return self.now_ns


def factory(oracle: Oracle) -> Callable[[EndpointConfig], Oracle]:
    def create(endpoint: EndpointConfig) -> Oracle:
        return oracle

    return create
