import json

import pytest
import respx
from pydantic import BaseModel

from app.ai.llm.errors import (
    LLMRequestError,
    LLMUnavailableError,
    StructuredOutputUnsupportedError,
)
from app.ai.llm.openai_compat import OpenAICompatibleProvider
from app.ai.llm.types import ChatMessage, GenerationOptions

BASE = "http://vllm.test:8000/v1"
MESSAGES = [ChatMessage(role="system", content="Be brief."), ChatMessage(role="user", content="Hi")]


class Answer(BaseModel):
    category: str


@pytest.fixture
async def provider() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(BASE, api_key="sk-test")


def _completion(content: str) -> dict[str, object]:
    return {
        "model": "served-model",
        "choices": [
            {"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 3},
    }


@respx.mock
async def test_complete_sends_response_format_and_auth(provider: OpenAICompatibleProvider) -> None:
    route = respx.post(f"{BASE}/chat/completions").respond(json=_completion('{"category":"x"}'))

    result = await provider.complete(
        MESSAGES,
        model="m",
        schema=Answer,
        options=GenerationOptions(temperature=0.0, max_tokens=50, context_tokens=4096),
    )

    request = route.calls.last.request
    payload = json.loads(request.content)
    assert request.headers["Authorization"] == "Bearer sk-test"
    assert payload["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "Answer", "schema": Answer.model_json_schema()},
    }
    assert payload["temperature"] == 0.0
    assert payload["max_tokens"] == 50
    assert "num_ctx" not in json.dumps(payload)
    assert result.content == '{"category":"x"}'
    assert result.model == "served-model"
    assert result.usage.prompt_tokens == 20
    assert result.usage.completion_tokens == 3


@respx.mock
async def test_no_auth_header_without_key() -> None:
    route = respx.post(f"{BASE}/chat/completions").respond(json=_completion("hi"))

    await OpenAICompatibleProvider(BASE).complete(MESSAGES, model="m")

    assert "Authorization" not in route.calls.last.request.headers
    assert "response_format" not in json.loads(route.calls.last.request.content)


@respx.mock
async def test_rejected_schema_is_reported_as_unsupported(
    provider: OpenAICompatibleProvider,
) -> None:
    respx.post(f"{BASE}/chat/completions").respond(400, json={"error": "unknown field"})

    with pytest.raises(StructuredOutputUnsupportedError):
        await provider.complete(MESSAGES, model="m", schema=Answer)


@respx.mock
async def test_bad_request_without_schema(provider: OpenAICompatibleProvider) -> None:
    respx.post(f"{BASE}/chat/completions").respond(400)

    with pytest.raises(LLMRequestError) as excinfo:
        await provider.complete(MESSAGES, model="m")

    assert not isinstance(excinfo.value, StructuredOutputUnsupportedError)


@respx.mock
async def test_rate_limit_means_unavailable(provider: OpenAICompatibleProvider) -> None:
    respx.post(f"{BASE}/chat/completions").respond(429)

    with pytest.raises(LLMUnavailableError):
        await provider.complete(MESSAGES, model="m")


@respx.mock
async def test_malformed_response(provider: OpenAICompatibleProvider) -> None:
    respx.post(f"{BASE}/chat/completions").respond(json={"choices": []})

    with pytest.raises(LLMUnavailableError):
        await provider.complete(MESSAGES, model="m")


@respx.mock
async def test_stream_parses_server_sent_events(provider: OpenAICompatibleProvider) -> None:
    events = [
        {"choices": [{"delta": {"role": "assistant"}}]},
        {"choices": [{"delta": {"content": "Hel"}}]},
        {"choices": [{"delta": {"content": "lo"}}]},
    ]
    body = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
    respx.post(f"{BASE}/chat/completions").respond(
        content=body, headers={"content-type": "text/event-stream"}
    )

    chunks = [chunk async for chunk in provider.stream(MESSAGES, model="m")]

    assert chunks == ["Hel", "lo"]


@respx.mock
async def test_embed_orders_by_index(provider: OpenAICompatibleProvider) -> None:
    respx.post(f"{BASE}/embeddings").respond(
        json={"data": [{"index": 1, "embedding": [2.0]}, {"index": 0, "embedding": [1.0]}]}
    )

    assert await provider.embed(["a", "b"], model="e") == [[1.0], [2.0]]


@respx.mock
async def test_list_models(provider: OpenAICompatibleProvider) -> None:
    respx.get(f"{BASE}/models").respond(json={"data": [{"id": "a"}, {"id": "b"}]})

    assert await provider.list_models() == ["a", "b"]
    await provider.aclose()
