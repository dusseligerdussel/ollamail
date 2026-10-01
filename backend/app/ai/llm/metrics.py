"""Per-call metrics without content: model, prompt version, duration, token counts."""

from dataclasses import asdict, dataclass
from typing import Literal, Protocol

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


class MetricsSink(Protocol):
    def record(self, metrics: LLMCallMetrics) -> None: ...


class LoggingMetricsSink:
    """Default sink: one ``llm_call`` log event per call (no content by construction)."""

    def record(self, metrics: LLMCallMetrics) -> None:
        log.info("llm_call", **asdict(metrics))
