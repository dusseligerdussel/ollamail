"""Per-call metrics without content: model, prompt version, duration, token counts.

Every call is logged (``llm_call``) and counted for Prometheus (``/metrics``, see
``app.core.metrics``). Labels are configuration values only: task, operation, endpoint
name, provider, model.
"""

from dataclasses import asdict, dataclass
from typing import Literal, Protocol

from prometheus_client import Counter, Histogram

from app.core.logging import get_logger

Operation = Literal["complete", "structured", "stream", "embed"]

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class LLMCallMetrics:
    task: str
    operation: Operation
    endpoint: str
    provider: str
    model: str
    prompt_version: str | None
    duration_ms: int
    success: bool
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    # Structured output: number of model calls including retries.
    attempts: int = 1
    error_type: str | None = None
    # Monotonic duration in nanoseconds; ``duration_ms`` is rounded and is 0 for calls
    # shorter than half a millisecond. ``None`` for metrics built without it.
    duration_ns: int | None = None

    @property
    def seconds(self) -> float:
        """Call duration at the best available resolution."""
        if self.duration_ns is not None:
            return self.duration_ns / 1_000_000_000
        return self.duration_ms / 1000


class MetricsSink(Protocol):
    def record(self, metrics: LLMCallMetrics) -> None: ...


_LABELS = ("task", "operation", "endpoint", "provider", "model")

LLM_DURATION = Histogram(
    "ollamail_llm_request_duration_seconds",
    "Duration of LLM calls, including structured-output retries.",
    [*_LABELS, "outcome"],
    buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 60, 120, 300, 600),
)
LLM_TOKENS = Counter(
    "ollamail_llm_tokens",
    "Tokens reported by the model.",
    [*_LABELS, "kind"],
)
LLM_TOKENS_PER_SECOND = Histogram(
    "ollamail_llm_completion_tokens_per_second",
    "Generated tokens per second of successful LLM calls (wall-clock, incl. prompt).",
    ["endpoint", "provider", "model"],
    buckets=(1, 2, 5, 10, 20, 30, 50, 75, 100, 150, 250, 500),
)
LLM_ERRORS = Counter(
    "ollamail_llm_errors",
    "Failed LLM calls by exception type.",
    [*_LABELS, "error_type"],
)


def observe(metrics: LLMCallMetrics) -> None:
    """Count one call for Prometheus."""
    labels = (metrics.task, metrics.operation, metrics.endpoint, metrics.provider, metrics.model)
    seconds = metrics.seconds
    outcome = "success" if metrics.success else "error"
    LLM_DURATION.labels(*labels, outcome).observe(seconds)
    if metrics.prompt_tokens:
        LLM_TOKENS.labels(*labels, "prompt").inc(metrics.prompt_tokens)
    if metrics.completion_tokens:
        LLM_TOKENS.labels(*labels, "completion").inc(metrics.completion_tokens)
        if metrics.success and seconds > 0:
            LLM_TOKENS_PER_SECOND.labels(metrics.endpoint, metrics.provider, metrics.model).observe(
                metrics.completion_tokens / seconds
            )
    if not metrics.success:
        LLM_ERRORS.labels(*labels, metrics.error_type or "unknown").inc()


class LoggingMetricsSink:
    """Default sink: one ``llm_call`` log event per call (no content by construction)
    and the Prometheus counters."""

    def record(self, metrics: LLMCallMetrics) -> None:
        log.info("llm_call", **asdict(metrics))
        observe(metrics)
