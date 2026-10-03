"""Metrics of the evaluation: pure functions, no model and no database.

* Triage: accuracy and confusion matrix.
* Todos: precision and recall; a predicted todo matches an expected one by title (fuzzy)
  or keyword, the due date is compared exactly.
* RAG: source found among the first k sources (Recall@k), answer correct by keyword rules.
* Calls: latency and tokens per second from the gateway's per-call metrics.
"""

import math
import re
import statistics
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from difflib import SequenceMatcher

from app.ai.llm.errors import LLMTimeoutError
from app.ai.llm.metrics import LLMCallMetrics

# Error type the gateway records for calls past their deadline or HTTP read timeout.
TIMEOUT_ERROR = LLMTimeoutError.__name__

# Fuzzy title similarity from which a predicted todo counts as the expected one.
TITLE_THRESHOLD = 0.6
_WORD = re.compile(r"\w+")


# --- triage --------------------------------------------------------------------------


@dataclass
class Confusion:
    """Counts of (expected, predicted) pairs; ``predicted`` is ``error`` on failures."""

    counts: Counter[tuple[str, str]] = field(default_factory=Counter)

    def add(self, expected: str, predicted: str) -> None:
        self.counts[(expected, predicted)] += 1

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def correct(self) -> int:
        return sum(n for (e, p), n in self.counts.items() if e == p)

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0

    def labels(self, order: Sequence[str] = ()) -> list[str]:
        seen = {label for pair in self.counts for label in pair}
        known = [label for label in order if label in seen]
        return known + sorted(seen - set(known))

    def matrix(self, order: Sequence[str] = ()) -> dict[str, dict[str, int]]:
        """``{expected: {predicted: count}}`` over all labels that occur."""
        labels = self.labels(order)
        return {e: {p: self.counts.get((e, p), 0) for p in labels} for e in labels}

    def per_class_recall(self, order: Sequence[str] = ()) -> dict[str, float]:
        result: dict[str, float] = {}
        for label in self.labels(order):
            total = sum(n for (e, _), n in self.counts.items() if e == label)
            if total:
                result[label] = self.counts.get((label, label), 0) / total
        return result


# --- todos ---------------------------------------------------------------------------


def normalize(text: str) -> str:
    """Lower case, without accents and punctuation (``Prüfen!`` → ``prufen``)."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(_WORD.findall(plain))


def title_similarity(a: str, b: str) -> float:
    """Similarity of two titles in [0, 1], robust to word order and small changes."""
    left, right = normalize(a), normalize(b)
    if not left or not right:
        return 0.0
    direct = SequenceMatcher(None, left, right).ratio()
    ordered = SequenceMatcher(
        None, " ".join(sorted(left.split())), " ".join(sorted(right.split()))
    ).ratio()
    return max(direct, ordered)


@dataclass(frozen=True)
class PredictedTodo:
    title: str
    description: str | None
    due: date | None


@dataclass(frozen=True)
class ExpectedTask:
    title: str
    keywords: tuple[str, ...]
    due: date | None


def todo_matches(expected: ExpectedTask, predicted: PredictedTodo) -> float:
    """Match score in [0, 1]: fuzzy title similarity, or 1 for a keyword hit; 0 below
    the threshold."""
    text = normalize(f"{predicted.title} {predicted.description or ''}")
    if any(normalize(keyword) in text for keyword in expected.keywords if normalize(keyword)):
        return 1.0
    score = title_similarity(expected.title, predicted.title)
    return score if score >= TITLE_THRESHOLD else 0.0


@dataclass
class TodoScore:
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    # Matched todos whose expected due date was compared, and how many were equal.
    due_checked: int = 0
    due_correct: int = 0

    def __iadd__(self, other: "TodoScore") -> "TodoScore":
        self.true_positives += other.true_positives
        self.false_positives += other.false_positives
        self.false_negatives += other.false_negatives
        self.due_checked += other.due_checked
        self.due_correct += other.due_correct
        return self

    @property
    def precision(self) -> float:
        found = self.true_positives + self.false_positives
        return self.true_positives / found if found else 1.0

    @property
    def recall(self) -> float:
        wanted = self.true_positives + self.false_negatives
        return self.true_positives / wanted if wanted else 1.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0

    @property
    def due_accuracy(self) -> float:
        return self.due_correct / self.due_checked if self.due_checked else 1.0


def score_todos(expected: Sequence[ExpectedTask], predicted: Sequence[PredictedTodo]) -> TodoScore:
    """Greedy one-to-one matching, best score first. Every matched pair checks the due
    date exactly: both ``None`` counts as correct."""
    pairs = sorted(
        (
            (todo_matches(e, p), i, j)
            for i, e in enumerate(expected)
            for j, p in enumerate(predicted)
        ),
        reverse=True,
    )
    used_expected: set[int] = set()
    used_predicted: set[int] = set()
    score = TodoScore()
    for value, i, j in pairs:
        if value <= 0 or i in used_expected or j in used_predicted:
            continue
        used_expected.add(i)
        used_predicted.add(j)
        score.true_positives += 1
        score.due_checked += 1
        score.due_correct += expected[i].due == predicted[j].due
    score.false_negatives = len(expected) - len(used_expected)
    score.false_positives = len(predicted) - len(used_predicted)
    return score


# --- rag -----------------------------------------------------------------------------


def source_rank(sources: Sequence[str], expected: Iterable[str]) -> int | None:
    """1-based rank of the first expected mail among the retrieved sources (several
    chunks of one mail count once), ``None`` if none was retrieved."""
    wanted = set(expected)
    seen: list[str] = []
    for source in sources:
        if source not in seen:
            seen.append(source)
            if source in wanted:
                return len(seen)
    return None


def recall_at_k(ranks: Sequence[int | None], k: int) -> float:
    return sum(1 for r in ranks if r is not None and r <= k) / len(ranks) if ranks else 0.0


def mean_reciprocal_rank(ranks: Sequence[int | None]) -> float:
    return sum(1 / r for r in ranks if r is not None) / len(ranks) if ranks else 0.0


def answer_contains(answer: str, groups: Sequence[Sequence[str]]) -> bool:
    """Every group has one alternative in ``answer`` (case- and accent-insensitive)."""
    text = normalize(answer)
    return all(any(normalize(option) in text for option in group) for group in groups)


# --- calls ---------------------------------------------------------------------------


def percentile(values: Sequence[float], share: float) -> float:
    """Nearest-rank percentile (``share`` in (0, 1])."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(share * len(ordered)) - 1)
    return ordered[index]


@dataclass(frozen=True)
class CallStats:
    calls: int
    failed: int
    # Calls that timed out (part of ``failed``).
    timeouts: int
    seconds_mean: float
    seconds_p50: float
    seconds_p95: float
    prompt_tokens: int
    completion_tokens: int
    # Generated tokens per second of call time, over calls that report token counts.
    tokens_per_second: float | None
    # Prompt plus generated tokens per second of call time (CPU: prompt processing
    # dominates for long prompts and short answers).
    processed_tokens_per_second: float | None
    # Streams report no token counts; their tokens are estimated from the text.
    estimated: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "calls": self.calls,
            "failed": self.failed,
            "timeouts": self.timeouts,
            "seconds_mean": round(self.seconds_mean, 2),
            "seconds_p50": round(self.seconds_p50, 2),
            "seconds_p95": round(self.seconds_p95, 2),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "tokens_per_second": (
                round(self.tokens_per_second, 2) if self.tokens_per_second is not None else None
            ),
            "processed_tokens_per_second": (
                round(self.processed_tokens_per_second, 2)
                if self.processed_tokens_per_second is not None
                else None
            ),
            "tokens_estimated": self.estimated,
        }


def call_stats(calls: Sequence[LLMCallMetrics]) -> CallStats:
    seconds = [c.duration_ms / 1000 for c in calls]
    with_tokens = [c for c in calls if c.success and c.completion_tokens]
    generated = sum(c.completion_tokens or 0 for c in with_tokens)
    processed = generated + sum(c.prompt_tokens or 0 for c in with_tokens)
    busy = sum(c.duration_ms for c in with_tokens) / 1000
    return CallStats(
        calls=len(calls),
        failed=sum(not c.success for c in calls),
        timeouts=sum(c.error_type == TIMEOUT_ERROR for c in calls),
        seconds_mean=statistics.fmean(seconds) if seconds else 0.0,
        seconds_p50=percentile(seconds, 0.5),
        seconds_p95=percentile(seconds, 0.95),
        prompt_tokens=sum(c.prompt_tokens or 0 for c in calls),
        completion_tokens=sum(c.completion_tokens or 0 for c in calls),
        tokens_per_second=generated / busy if busy > 0 and generated else None,
        processed_tokens_per_second=processed / busy if busy > 0 and processed else None,
        estimated=any(c.operation == "stream" for c in calls),
    )


@dataclass
class RecordingSink:
    """``MetricsSink`` that keeps every call (no content, see ``LLMCallMetrics``) and
    still logs nothing: the evaluation reports the numbers itself."""

    calls: list[LLMCallMetrics] = field(default_factory=list)

    def record(self, metrics: LLMCallMetrics) -> None:
        self.calls.append(metrics)

    def for_task(self, task: str, *, since: int = 0) -> list[LLMCallMetrics]:
        return [c for c in self.calls[since:] if c.task == task]
