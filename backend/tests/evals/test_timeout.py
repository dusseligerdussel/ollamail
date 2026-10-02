"""Time limit per model call in evaluation runs."""

import asyncio
from collections.abc import AsyncIterator, Sequence

import pytest
from pydantic import BaseModel

from app.ai.llm import (
    ChatMessage,
    EnvConfigResolver,
    GenerationOptions,
    LLMGateway,
    LLMResult,
    LLMTask,
    LLMUnavailableError,
)
from app.core.config import LLMSettings, Settings
from app.evals.dataset import Dataset
from app.evals.metrics import RecordingSink, call_stats
from app.evals.runner import RunOptions, run
from app.evals.timeout import TIMEOUT_ERROR, CallTimeoutError, time_limited
from tests.evals.conftest import Oracle, factory

MESSAGES = [ChatMessage(role="user", content="hi")]


class Slow:
    """Answers after ``delay`` seconds; streams one chunk per ``delay``."""

    def __init__(self, delay: float) -> None:
        self.delay = delay

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str,
        schema: type[BaseModel] | None = None,
        options: GenerationOptions | None = None,
    ) -> LLMResult:
        await asyncio.sleep(self.delay)
        return LLMResult(content="done", model=model)

    async def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str,
        options: GenerationOptions | None = None,
    ) -> AsyncIterator[str]:
        for word in ("a", "b", "c"):
            await asyncio.sleep(self.delay)
            yield word

    async def embed(self, texts: Sequence[str], *, model: str) -> list[list[float]]:
        return [[1.0] for _ in texts]

    async def list_models(self) -> list[str]:
        return []

    async def aclose(self) -> None:
        pass


def _gateway(delay: float, seconds: float | None, sink: RecordingSink) -> LLMGateway:
    return LLMGateway(
        EnvConfigResolver(LLMSettings(default_chat_model="m")),
        provider_factory=time_limited(lambda _: Slow(delay), seconds),
        metrics=sink,
    )


async def test_calls_over_the_limit_fail_as_timeouts() -> None:
    sink = RecordingSink()
    llm = _gateway(0.2, 0.05, sink)

    with pytest.raises(CallTimeoutError) as raised:
        await llm.complete(LLMTask.TODOS, MESSAGES)
    with pytest.raises(CallTimeoutError):
        async for _ in llm.stream(LLMTask.RAG_CHAT, MESSAGES):
            pass

    # Features see an unavailable model; the metrics name the timeout.
    assert isinstance(raised.value, LLMUnavailableError)
    assert [c.error_type for c in sink.calls] == [TIMEOUT_ERROR, TIMEOUT_ERROR]
    stats = call_stats(sink.calls)
    assert (stats.failed, stats.timeouts) == (2, 2)


async def test_the_limit_covers_a_whole_stream() -> None:
    llm = _gateway(0.03, 0.07, RecordingSink())
    received: list[str] = []
    with pytest.raises(CallTimeoutError):
        async for chunk in llm.stream(LLMTask.RAG_CHAT, MESSAGES):
            received.append(chunk)
    assert received == ["a", "b"]


async def test_fast_calls_and_no_limit_pass() -> None:
    sink = RecordingSink()
    assert (await _gateway(0.0, 1.0, sink).complete(LLMTask.TRIAGE, MESSAGES)).content == "done"
    assert (await _gateway(0.01, None, sink).complete(LLMTask.TRIAGE, MESSAGES)).content == "done"
    assert call_stats(sink.calls).timeouts == 0


class SlowTodos(Oracle):
    """The perfect model, but the todo extraction never finishes in time."""

    async def complete(self, messages, *, model, schema=None, options=None):  # type: ignore[no-untyped-def]
        if schema is not None and schema.__name__ == "TodoExtraction":
            await asyncio.sleep(1)
        return await super().complete(messages, model=model, schema=schema, options=options)


async def test_report_counts_timeouts(dataset: Dataset) -> None:
    oracle = SlowTodos(dataset)
    report = await run(
        dataset.subset(limit=20),
        RunOptions(
            models=["perfect:1b"],
            stages=("triage", "todos"),
            settings=Settings(llm=LLMSettings(structured_output_retries=0)),
            provider_factory=factory(oracle),
            timeout=0.05,
        ),
    )

    (result,) = report.models
    todos = result.stages["todos"]
    assert todos["errors"] == todos["mails"] > 0
    assert result.calls["todos"]["timeouts"] == todos["mails"]
    assert result.calls["triage"]["timeouts"] == 0
    assert result.timeouts["timed_out"] == todos["mails"]
    assert 0 < result.timeouts["rate"] < 1
    assert report.run["call_timeout_seconds"] == 0.05
    markdown = report.to_markdown()
    assert "Time limit per model call" in markdown
    assert f"{todos['mails']}/{result.timeouts['calls']}" in markdown
