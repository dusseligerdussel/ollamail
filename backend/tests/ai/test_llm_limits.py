"""Answer limits and call deadlines of the gateway (#132): every call is bounded, also
for providers that never finish."""

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field

import httpx
import pytest
import respx
from pydantic import BaseModel

from app.ai.llm.config import EnvConfigResolver, ResolvedConfig
from app.ai.llm.errors import LLMTimeoutError, LLMUnavailableError
from app.ai.llm.gateway import LLMGateway
from app.ai.llm.ollama import OllamaProvider
from app.ai.llm.types import ChatMessage, GenerationOptions, LLMResult, LLMTask
from app.core.config import LLMSettings
from tests.ai.fakes import FakeFactory, FakeProvider, RecordingSink

MAIL = "Dear Bob, the confidential merger is off."
MESSAGES = [ChatMessage(role="user", content=MAIL)]
# Short enough for fast tests, long enough for a scheduler hiccup not to matter.
DEADLINE = 0.2


class Todos(BaseModel):
    todos: list[str]


@dataclass
class EndlessProvider:
    """Never finishes: ``complete`` hangs, ``stream`` yields tokens forever."""

    started: int = 0
    stream_closed: bool = False
    options: list[GenerationOptions | None] = field(default_factory=list)

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str,
        schema: type[BaseModel] | None = None,
        options: GenerationOptions | None = None,
    ) -> LLMResult:
        self.started += 1
        self.options.append(options)
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str,
        options: GenerationOptions | None = None,
    ) -> AsyncIterator[str]:
        self.started += 1
        self.options.append(options)
        try:
            while True:
                await asyncio.sleep(0.005)
                yield "token "
        finally:
            self.stream_closed = True

    async def embed(self, texts: Sequence[str], *, model: str) -> list[list[float]]:
        return [[0.0] for _ in texts]

    async def list_models(self) -> list[str]:
        return []

    async def aclose(self) -> None:
        return None


def _gateway(
    settings: LLMSettings, provider: object, *, concurrency: int | None = None
) -> tuple[LLMGateway, RecordingSink]:
    sink = RecordingSink()

    async def limit() -> int:
        assert concurrency is not None
        return concurrency

    gateway = LLMGateway(
        EnvConfigResolver(settings),
        provider_factory=lambda endpoint: provider,  # type: ignore[arg-type,return-value]
        metrics=sink,
        concurrency=limit if concurrency is not None else None,
    )
    return gateway, sink


def _settings(**values: object) -> LLMSettings:
    return LLMSettings.model_validate({"default_chat_model": "chat:1b", **values})


# --- answer limit --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("task", "expected"),
    [(LLMTask.TODOS, 800), (LLMTask.TRIAGE, 1024), (LLMTask.RAG_CHAT, 1024)],
)
async def test_gateway_sets_a_default_answer_limit_per_task(task: LLMTask, expected: int) -> None:
    provider = FakeProvider(answers=["ok"])
    gateway, _, _ = _fake_gateway(_settings(), provider)

    await gateway.complete(task, MESSAGES)

    options = provider.calls[0].options
    assert options is not None
    assert options.max_tokens == expected


async def test_explicit_answer_limit_of_the_feature_wins() -> None:
    provider = FakeProvider(answers=["ok"])
    gateway, _, _ = _fake_gateway(_settings(task_todos_max_tokens=500), provider)

    await gateway.complete(LLMTask.TODOS, MESSAGES, options=GenerationOptions(max_tokens=64))

    options = provider.calls[0].options
    assert options is not None
    assert options.max_tokens == 64


def test_answer_limit_resolution_order() -> None:
    default = ResolvedConfig(_settings())
    assert default.max_output_tokens(LLMTask.TODOS) == 800
    assert default.max_output_tokens(LLMTask.DIGEST) == 1024

    # A global value applies to every task without an own value, built-in ones included.
    global_ = ResolvedConfig(_settings(max_output_tokens=600))
    assert global_.max_output_tokens(LLMTask.TODOS) == 600
    assert global_.max_output_tokens(LLMTask.TRIAGE) == 600

    per_task = ResolvedConfig(_settings(max_output_tokens=600, task_todos_max_tokens=300))
    assert per_task.max_output_tokens(LLMTask.TODOS) == 300
    assert per_task.max_output_tokens(LLMTask.TRIAGE) == 600


def test_answer_limit_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMAIL_LLM_TASK_TODOS_MAX_TOKENS", "400")
    monkeypatch.setenv("OLLAMAIL_LLM_TASK_TODOS_CALL_TIMEOUT", "90")
    config = ResolvedConfig(LLMSettings())
    assignment = config.assignment(LLMTask.TODOS)
    assert assignment.max_output_tokens == 400
    assert assignment.call_timeout == 90


@respx.mock
async def test_ollama_receives_num_predict_without_feature_limit() -> None:
    route = respx.post("http://ollama.test/api/chat").respond(
        json={"model": "chat:1b", "message": {"role": "assistant", "content": '{"todos": []}'}}
    )
    gateway = LLMGateway(
        EnvConfigResolver(_settings(base_url="http://ollama.test")), metrics=RecordingSink()
    )

    await gateway.complete_structured(LLMTask.TODOS, MESSAGES, Todos)

    payload = json.loads(route.calls.last.request.content)
    assert payload["options"]["num_predict"] == 800
    await gateway.aclose()


@respx.mock
async def test_openai_compatible_receives_max_tokens_without_feature_limit() -> None:
    route = respx.post("http://vllm.test/v1/chat/completions").respond(
        json={
            "model": "chat:1b",
            "choices": [{"message": {"content": '{"todos": []}'}, "finish_reason": "stop"}],
        }
    )
    settings = _settings(provider="openai_compatible", base_url="http://vllm.test/v1")
    gateway = LLMGateway(EnvConfigResolver(settings), metrics=RecordingSink())

    await gateway.complete_structured(LLMTask.TODOS, MESSAGES, Todos)

    payload = json.loads(route.calls.last.request.content)
    assert payload["max_tokens"] == 800
    await gateway.aclose()


# --- call deadline -------------------------------------------------------------------


def test_call_timeout_resolution_order() -> None:
    cpu = ResolvedConfig(_settings())
    assert cpu.task_call_timeout(LLMTask.TODOS) == 180
    assert cpu.task_call_timeout(LLMTask.DIGEST) == 360
    # Embeddings are bounded by the HTTP timeout only.
    assert cpu.task_call_timeout(LLMTask.EMBEDDINGS) is None

    gpu = ResolvedConfig(_settings(profile="gpu-consumer"))
    assert gpu.task_call_timeout(LLMTask.TODOS) == 60
    assert gpu.task_call_timeout(LLMTask.RAG_CHAT) == 120

    configured = ResolvedConfig(_settings(call_timeout=100, task_todos_call_timeout=30))
    assert configured.task_call_timeout(LLMTask.TODOS) == 30
    assert configured.task_call_timeout(LLMTask.DIGEST) == 100


async def test_hanging_completion_times_out_and_frees_the_slot() -> None:
    provider = EndlessProvider()
    gateway, sink = _gateway(_settings(task_triage_call_timeout=DEADLINE), provider, concurrency=1)

    for _ in range(2):
        # The second call only starts if the first one released the slot.
        with pytest.raises(LLMTimeoutError) as raised:
            await asyncio.wait_for(gateway.complete(LLMTask.TRIAGE, MESSAGES), timeout=5)
        assert isinstance(raised.value, LLMUnavailableError)
        assert MAIL not in str(raised.value)

    assert provider.started == 2
    assert [m.error_type for m in sink.records] == ["LLMTimeoutError", "LLMTimeoutError"]
    assert all(MAIL not in repr(m) for m in sink.records)


async def test_deadline_covers_all_structured_attempts() -> None:
    # Invalid JSON would be retried twice; the deadline ends the call anyway.
    provider = FakeProvider(answers=["no json", "still none", "nope"])
    slow = _SlowProvider(provider, delay=DEADLINE / 2)
    gateway, sink = _gateway(_settings(task_todos_call_timeout=DEADLINE), slow)

    with pytest.raises(LLMTimeoutError):
        await asyncio.wait_for(
            gateway.complete_structured(LLMTask.TODOS, MESSAGES, Todos), timeout=5
        )

    assert len(provider.calls) < 3
    (metrics,) = sink.records
    assert metrics.operation == "structured"
    assert metrics.error_type == "LLMTimeoutError"


async def test_endless_stream_is_cut_off_at_the_deadline() -> None:
    provider = EndlessProvider()
    gateway, sink = _gateway(_settings(task_rag_chat_call_timeout=DEADLINE), provider)
    received: list[str] = []

    async def consume() -> None:
        async for chunk in gateway.stream(LLMTask.RAG_CHAT, MESSAGES):
            received.append(chunk)

    with pytest.raises(LLMTimeoutError):
        await asyncio.wait_for(consume(), timeout=5)

    assert received, "chunks before the deadline reach the caller"
    assert provider.stream_closed, "the provider stream (HTTP connection) is closed"
    options = provider.options[0]
    assert options is not None
    assert options.max_tokens == 1024
    (metrics,) = sink.records
    assert metrics.operation == "stream"
    assert metrics.error_type == "LLMTimeoutError"


async def test_slow_consumer_is_not_cancelled_between_chunks() -> None:
    provider = FakeProvider(answers=["one two three"])
    gateway, sink = _gateway(_settings(task_rag_chat_call_timeout=5), provider)
    received: list[str] = []

    async for chunk in gateway.stream(LLMTask.RAG_CHAT, MESSAGES):
        # The consumer's own awaits (e.g. database writes) run outside the timeout scope.
        await asyncio.sleep(0.01)
        received.append(chunk)

    assert "".join(received).split() == ["one", "two", "three"]
    assert sink.records[0].success is True


async def test_no_deadline_for_embeddings() -> None:
    provider = FakeProvider()
    gateway, _ = _gateway(_settings(call_timeout=DEADLINE), provider)

    assert await gateway.embed(["a"]) == [[1.0, 0.0]]


# --- HTTP timeouts -------------------------------------------------------------------


@respx.mock
async def test_read_timeout_is_a_timeout_error() -> None:
    respx.post("http://ollama.test/api/chat").mock(side_effect=httpx.ReadTimeout("slow"))
    provider = OllamaProvider("http://ollama.test")

    with pytest.raises(LLMTimeoutError):
        await provider.complete(MESSAGES, model="m")
    await provider.aclose()


@respx.mock
async def test_connect_timeout_means_unreachable() -> None:
    respx.post("http://ollama.test/api/chat").mock(side_effect=httpx.ConnectTimeout("down"))
    provider = OllamaProvider("http://ollama.test")

    with pytest.raises(LLMUnavailableError) as raised:
        await provider.complete(MESSAGES, model="m")
    assert not isinstance(raised.value, LLMTimeoutError)
    await provider.aclose()


# --- helpers -------------------------------------------------------------------------


def _fake_gateway(
    settings: LLMSettings, provider: FakeProvider
) -> tuple[LLMGateway, FakeFactory, RecordingSink]:
    factory = FakeFactory(default=provider)
    sink = RecordingSink()
    gateway = LLMGateway(EnvConfigResolver(settings), provider_factory=factory, metrics=sink)
    return gateway, factory, sink


@dataclass
class _SlowProvider:
    """Wraps a fake and delays every completion."""

    inner: FakeProvider
    delay: float

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str,
        schema: type[BaseModel] | None = None,
        options: GenerationOptions | None = None,
    ) -> LLMResult:
        await asyncio.sleep(self.delay)
        return await self.inner.complete(messages, model=model, schema=schema, options=options)

    async def aclose(self) -> None:
        return None


async def test_stream_closed_by_caller_closes_the_provider_stream() -> None:
    provider = EndlessProvider()
    gateway, _ = _gateway(_settings(), provider)

    stream = gateway.stream(LLMTask.RAG_CHAT, MESSAGES)
    assert await anext(stream)
    await stream.aclose()

    assert provider.stream_closed
