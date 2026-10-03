import json

import httpx
import pytest
import respx
from pydantic import BaseModel

from app.ai.llm.errors import LLMRequestError, LLMUnavailableError, ModelNotAvailableError
from app.ai.llm.ollama import OllamaProvider
from app.ai.llm.types import ChatMessage, GenerationOptions

BASE = "http://ollama.test:11434"
MESSAGES = [ChatMessage(role="user", content="Hello")]


class Answer(BaseModel):
    category: str


@pytest.fixture
async def provider() -> OllamaProvider:
    return OllamaProvider(BASE)


@respx.mock
async def test_complete_sends_schema_and_context(provider: OllamaProvider) -> None:
    route = respx.post(f"{BASE}/api/chat").respond(
        json={
            "model": "m:1b",
            "message": {"role": "assistant", "content": '{"category": "info"}'},
            "prompt_eval_count": 12,
            "eval_count": 7,
            "done_reason": "stop",
        }
    )

    result = await provider.complete(
        MESSAGES,
        model="m:1b",
        schema=Answer,
        options=GenerationOptions(temperature=0.1, max_tokens=100, context_tokens=8192),
    )

    payload = json.loads(route.calls.last.request.content)
    assert payload["model"] == "m:1b"
    assert payload["stream"] is False
    assert payload["messages"] == [{"role": "user", "content": "Hello"}]
    assert payload["format"] == Answer.model_json_schema()
    assert payload["options"] == {"temperature": 0.1, "num_predict": 100, "num_ctx": 8192}
    assert result.content == '{"category": "info"}'
    assert result.usage.prompt_tokens == 12
    assert result.usage.completion_tokens == 7
    assert result.finish_reason == "stop"


@respx.mock
async def test_complete_without_schema_has_no_format(provider: OllamaProvider) -> None:
    route = respx.post(f"{BASE}/api/chat").respond(
        json={"model": "m", "message": {"role": "assistant", "content": "hi"}}
    )

    await provider.complete(MESSAGES, model="m")

    payload = json.loads(route.calls.last.request.content)
    assert "format" not in payload
    assert "options" not in payload


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (404, ModelNotAvailableError),
        (400, LLMRequestError),
        (500, LLMUnavailableError),
        (503, LLMUnavailableError),
    ],
)
@respx.mock
async def test_http_errors_are_typed_without_body(
    provider: OllamaProvider, status: int, error: type[Exception]
) -> None:
    respx.post(f"{BASE}/api/chat").respond(status, json={"error": "echo: Hello secret mail"})

    with pytest.raises(error) as excinfo:
        await provider.complete(MESSAGES, model="m")

    assert "secret mail" not in str(excinfo.value)


@respx.mock
async def test_connection_errors_mean_unavailable(provider: OllamaProvider) -> None:
    respx.post(f"{BASE}/api/chat").mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(LLMUnavailableError):
        await provider.complete(MESSAGES, model="m")


@respx.mock
async def test_stream_yields_chunks(provider: OllamaProvider) -> None:
    lines = [
        {"message": {"role": "assistant", "content": "Hel"}, "done": False},
        {"message": {"role": "assistant", "content": "lo"}, "done": False},
        {"message": {"role": "assistant", "content": ""}, "done": True, "eval_count": 2},
    ]
    route = respx.post(f"{BASE}/api/chat").respond(
        content="\n".join(json.dumps(line) for line in lines) + "\n"
    )

    chunks = [chunk async for chunk in provider.stream(MESSAGES, model="m")]

    assert chunks == ["Hel", "lo"]
    assert json.loads(route.calls.last.request.content)["stream"] is True


@respx.mock
async def test_stream_error_line_raises(provider: OllamaProvider) -> None:
    respx.post(f"{BASE}/api/chat").respond(content='{"error": "out of memory"}\n')

    with pytest.raises(LLMUnavailableError):
        _ = [chunk async for chunk in provider.stream(MESSAGES, model="m")]


@respx.mock
async def test_stream_http_error(provider: OllamaProvider) -> None:
    respx.post(f"{BASE}/api/chat").respond(404, json={"error": "model not found"})

    with pytest.raises(ModelNotAvailableError):
        _ = [chunk async for chunk in provider.stream(MESSAGES, model="m")]


@respx.mock
async def test_embed(provider: OllamaProvider) -> None:
    route = respx.post(f"{BASE}/api/embed").respond(json={"embeddings": [[0.1, 0.2], [0.3, 0.4]]})

    vectors = await provider.embed(["a", "b"], model="bge-m3")

    assert vectors == [[0.1, 0.2], [0.3, 0.4]]
    assert json.loads(route.calls.last.request.content) == {"model": "bge-m3", "input": ["a", "b"]}
    assert await provider.embed([], model="bge-m3") == []


@respx.mock
async def test_embed_count_mismatch(provider: OllamaProvider) -> None:
    respx.post(f"{BASE}/api/embed").respond(json={"embeddings": [[0.1]]})

    with pytest.raises(LLMUnavailableError):
        await provider.embed(["a", "b"], model="bge-m3")


@respx.mock
async def test_list_models_normalizes_tags(provider: OllamaProvider) -> None:
    respx.get(f"{BASE}/api/tags").respond(
        json={"models": [{"name": "qwen2.5:3b"}, {"name": "bge-m3"}]}
    )

    assert await provider.list_models() == ["qwen2.5:3b", "bge-m3:latest"]


@respx.mock
async def test_pull(provider: OllamaProvider) -> None:
    route = respx.post(f"{BASE}/api/pull").respond(json={"status": "success"})

    await provider.pull("bge-m3")

    assert json.loads(route.calls.last.request.content) == {"model": "bge-m3", "stream": False}
    await provider.aclose()


def _ndjson(*lines: dict[str, object]) -> bytes:
    return "\n".join(json.dumps(line) for line in lines).encode()


@respx.mock
async def test_pull_progress_sums_layers(provider: OllamaProvider) -> None:
    route = respx.post(f"{BASE}/api/pull").respond(
        200,
        content=_ndjson(
            {"status": "pulling manifest"},
            {"status": "pulling a", "digest": "sha256:a", "total": 100, "completed": 40},
            {"status": "pulling b", "digest": "sha256:b", "total": 50},
            {"status": "pulling a", "digest": "sha256:a", "total": 100, "completed": 100},
            {"status": "pulling b", "digest": "sha256:b", "total": 50, "completed": 50},
            {"status": "success"},
        ),
    )

    progress = [p async for p in provider.pull_progress("qwen2.5:3b")]

    assert json.loads(route.calls[0].request.content) == {"model": "qwen2.5:3b", "stream": True}
    assert progress == [(40, 100), (40, 150), (100, 150), (150, 150)]


@respx.mock
async def test_pull_progress_error_hides_server_text(provider: OllamaProvider) -> None:
    respx.post(f"{BASE}/api/pull").respond(
        200, content=_ndjson({"error": "pull model manifest: file does not exist"})
    )

    with pytest.raises(ModelNotAvailableError) as info:
        _ = [p async for p in provider.pull_progress("nope:1b")]

    assert "manifest" not in str(info.value)


@respx.mock
async def test_pull_progress_without_success_fails(provider: OllamaProvider) -> None:
    respx.post(f"{BASE}/api/pull").respond(200, content=_ndjson({"status": "pulling manifest"}))

    with pytest.raises(LLMUnavailableError):
        _ = [p async for p in provider.pull_progress("qwen2.5:3b")]
