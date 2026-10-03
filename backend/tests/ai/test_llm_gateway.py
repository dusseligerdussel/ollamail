import asyncio

import pytest
from pydantic import BaseModel

from app.ai.llm.config import EnvConfigResolver, env_endpoints
from app.ai.llm.errors import (
    CloudLLMDisabledError,
    LLMNotReadyError,
    LLMOutputError,
    LLMRequestError,
    LLMUnavailableError,
    StructuredOutputUnsupportedError,
)
from app.ai.llm.gateway import LLMGateway, create_provider
from app.ai.llm.ollama import OllamaProvider
from app.ai.llm.openai_compat import OpenAICompatibleProvider
from app.ai.llm.types import ChatMessage, GenerationOptions, LLMTask
from app.core.config import LLMSettings
from tests.ai.fakes import FakeFactory, FakeProvider, RecordingSink

MAIL = "Dear Bob, the confidential merger is off."
MESSAGES = [ChatMessage(role="user", content=MAIL)]
CLOUD_ENDPOINTS = {
    "cloud": {"provider": "openai_compatible", "base_url": "https://x/v1", "is_cloud": True}
}


class Triage(BaseModel):
    category: str


def _gateway(
    settings: LLMSettings | None = None, **providers: FakeProvider
) -> tuple[LLMGateway, FakeFactory, RecordingSink]:
    factory = FakeFactory(**providers)
    sink = RecordingSink()
    resolver = EnvConfigResolver(settings or LLMSettings(default_chat_model="chat:1b"))
    return LLMGateway(resolver, provider_factory=factory, metrics=sink), factory, sink


async def test_complete_uses_assigned_model_and_records_metrics() -> None:
    provider = FakeProvider(answers=["ok"])
    gateway, _, sink = _gateway(default=provider)

    result = await gateway.complete(LLMTask.TRIAGE, MESSAGES, prompt_version="triage@1")

    assert result.content == "ok"
    assert provider.calls[0].model == "chat:1b"
    assert provider.calls[0].options is not None
    assert provider.calls[0].options.context_tokens == 8192
    (metrics,) = sink.records
    assert metrics.task == "triage"
    assert metrics.operation == "complete"
    assert metrics.model == "chat:1b"
    assert metrics.prompt_version == "triage@1"
    assert metrics.success is True
    assert metrics.prompt_tokens == 10
    assert metrics.completion_tokens == 5
    assert MAIL not in repr(metrics)


async def test_task_override_selects_other_endpoint() -> None:
    settings = LLMSettings.model_validate(
        {
            "endpoints": {"gpu": {"provider": "openai_compatible", "base_url": "http://g/v1"}},
            "task_rag_chat_endpoint": "gpu",
            "task_rag_chat_model": "big",
        }
    )
    gpu = FakeProvider(answers=["answer"])
    gateway, _, _ = _gateway(settings, gpu=gpu)

    await gateway.complete(LLMTask.RAG_CHAT, MESSAGES)

    assert gpu.calls[0].model == "big"


async def test_cloud_endpoint_is_blocked_unless_enabled() -> None:
    blocked = LLMSettings.model_validate(
        {"endpoints": CLOUD_ENDPOINTS, "task_digest_endpoint": "cloud"}
    )
    gateway, factory, sink = _gateway(blocked)

    with pytest.raises(CloudLLMDisabledError):
        await gateway.complete(LLMTask.DIGEST, MESSAGES)
    assert factory.created == []  # no client was even built
    assert sink.records == []

    allowed = blocked.model_copy(update={"cloud_enabled": True})
    cloud = FakeProvider(answers=["ok"])
    gateway, _, _ = _gateway(allowed, cloud=cloud)
    await gateway.complete(LLMTask.DIGEST, MESSAGES)
    assert len(cloud.calls) == 1


async def test_default_endpoint_marked_cloud_is_blocked() -> None:
    gateway, _, _ = _gateway(LLMSettings(is_cloud=True))

    with pytest.raises(CloudLLMDisabledError):
        await gateway.embed(["text"])


async def test_long_mail_is_shortened_to_context() -> None:
    provider = FakeProvider(answers=["ok"])
    settings = LLMSettings(default_chat_model="m", context_tokens=1024)
    gateway, _, _ = _gateway(settings, default=provider)

    await gateway.complete(
        LLMTask.TODOS,
        [ChatMessage(role="user", content="word " * 10_000)],
        options=GenerationOptions(max_tokens=200),
    )

    sent = provider.calls[0].messages[0].content
    assert len(sent) < 3 * 1024


async def test_structured_returns_validated_object() -> None:
    provider = FakeProvider(answers=["garbage", '{"category": "info"}'])
    gateway, _, sink = _gateway(default=provider)

    value = await gateway.complete_structured(LLMTask.TRIAGE, MESSAGES, Triage)

    assert value == Triage(category="info")
    assert sink.records[0].operation == "structured"
    assert sink.records[0].attempts == 2
    assert sink.records[0].prompt_tokens == 20


async def test_structured_failure_after_retries() -> None:
    provider = FakeProvider(answers=["a", "b"])
    settings = LLMSettings(default_chat_model="m", structured_output_retries=1)
    gateway, _, sink = _gateway(settings, default=provider)

    with pytest.raises(LLMOutputError):
        await gateway.complete_structured(LLMTask.TRIAGE, MESSAGES, Triage)

    (metrics,) = sink.records
    assert metrics.success is False
    assert metrics.error_type == "LLMOutputError"
    assert metrics.attempts == 2


async def test_structured_falls_back_to_prompting_and_remembers() -> None:
    provider = FakeProvider(
        answers=[
            StructuredOutputUnsupportedError("HTTP 400", status_code=400),
            '{"category": "a"}',
            '{"category": "b"}',
        ]
    )
    gateway, _, _ = _gateway(default=provider)

    assert await gateway.complete_structured(LLMTask.TRIAGE, MESSAGES, Triage) == Triage(
        category="a"
    )
    assert await gateway.complete_structured(LLMTask.TRIAGE, MESSAGES, Triage) == Triage(
        category="b"
    )

    assert [call.schema for call in provider.calls] == [Triage, None, None]


async def test_prompt_mode_from_config() -> None:
    provider = FakeProvider(answers=['{"category": "a"}'])
    settings = LLMSettings(default_chat_model="m", structured_output="prompt")
    gateway, _, _ = _gateway(settings, default=provider)

    await gateway.complete_structured(LLMTask.TRIAGE, MESSAGES, Triage)

    assert provider.calls[0].schema is None


async def test_stream_records_estimated_usage() -> None:
    provider = FakeProvider(answers=["one two three"])
    gateway, _, sink = _gateway(default=provider)

    chunks = [c async for c in gateway.stream(LLMTask.RAG_CHAT, MESSAGES, prompt_version="rag@1")]

    assert "".join(chunks).split() == ["one", "two", "three"]
    (metrics,) = sink.records
    assert metrics.operation == "stream"
    assert metrics.success is True
    assert metrics.completion_tokens is not None
    assert metrics.completion_tokens > 0


async def test_embed_uses_embedding_model() -> None:
    gateway, _, sink = _gateway(LLMSettings(profile="cpu"))

    vectors = await gateway.embed(["ab", "abcd"])

    assert vectors == [[2.0, 0.0], [4.0, 0.0]]
    assert sink.records[0].model == "bge-m3"
    assert sink.records[0].task == "embeddings"


async def test_provider_is_reused_and_closed() -> None:
    gateway, factory, _ = _gateway(default=FakeProvider(answers=["a", "b"]))

    await gateway.complete(LLMTask.TRIAGE, MESSAGES)
    await gateway.complete(LLMTask.TODOS, MESSAGES)
    await gateway.aclose()

    assert len(factory.created) == 1
    assert factory.providers["default"].closed is True


async def test_ready_when_all_models_present() -> None:
    provider = FakeProvider(models=["chat:1b", "bge-m3:latest"])
    gateway, _, _ = _gateway(LLMSettings(default_chat_model="chat:1b"), default=provider)

    await gateway.check_ready()


async def test_not_ready_when_model_missing() -> None:
    provider = FakeProvider(models=["chat:1b"])
    gateway, _, _ = _gateway(LLMSettings(default_chat_model="chat:1b"), default=provider)

    with pytest.raises(LLMNotReadyError, match="bge-m3"):
        await gateway.check_ready()


async def test_not_ready_when_task_needs_disabled_cloud() -> None:
    settings = LLMSettings.model_validate(
        {"endpoints": CLOUD_ENDPOINTS, "task_digest_endpoint": "cloud"}
    )
    gateway, _, _ = _gateway(settings, default=FakeProvider(models=["qwen2.5:3b", "bge-m3"]))

    with pytest.raises(LLMNotReadyError):
        await gateway.check_ready()


def test_create_provider_by_kind() -> None:
    settings = LLMSettings.model_validate(
        {"endpoints": {"v": {"provider": "openai_compatible", "base_url": "http://v/v1"}}}
    )
    endpoints = env_endpoints(settings)

    assert isinstance(create_provider(endpoints["default"]), OllamaProvider)
    assert isinstance(create_provider(endpoints["v"]), OpenAICompatibleProvider)


async def test_model_status_per_task() -> None:
    settings = LLMSettings.model_validate(
        {
            "default_chat_model": "chat:1b",
            "endpoints": {
                "down": {"provider": "openai_compatible", "base_url": "http://down/v1"},
                **CLOUD_ENDPOINTS,
            },
            "task_rag_chat_endpoint": "down",
            "task_digest_endpoint": "cloud",
        }
    )
    down = FakeProvider()

    async def unreachable() -> list[str]:
        raise LLMUnavailableError("LLM endpoint unreachable")

    down.list_models = unreachable  # type: ignore[method-assign]
    gateway, _, _ = _gateway(settings, default=FakeProvider(models=["chat:1b"]), down=down)

    states = {status.task: status.state for status in await gateway.model_status()}

    assert states == {
        LLMTask.TRIAGE: "installed",
        LLMTask.TODOS: "installed",
        LLMTask.DIGEST: "disabled",
        LLMTask.RAG_CHAT: "unreachable",
        LLMTask.REPLY_DRAFT: "installed",
        LLMTask.EMBEDDINGS: "missing",
    }


async def test_model_status_bounds_slow_endpoints() -> None:
    slow = FakeProvider()

    async def hang() -> list[str]:
        await asyncio.sleep(10)
        return []

    slow.list_models = hang  # type: ignore[method-assign]
    gateway, _, _ = _gateway(default=slow)

    statuses = await gateway.model_status(wait=0.05)

    assert {status.state for status in statuses} == {"unreachable"}


async def test_pull_model_only_for_assigned_ollama_models() -> None:
    gateway, _, _ = _gateway(default=FakeProvider())

    with pytest.raises(LLMRequestError, match="not assigned"):
        _ = [p async for p in gateway.pull_model("default", "other:7b")]
    # Fakes are not Ollama providers: they cannot download.
    with pytest.raises(LLMRequestError, match="cannot download"):
        _ = [p async for p in gateway.pull_model("default", "chat:1b")]
