"""Ollama via its native REST API (``/api/chat``, ``/api/embed``, ``/api/tags``)."""

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx
from pydantic import BaseModel

from app.ai.llm._http import (
    build_client,
    json_object,
    raise_for_status,
    stream_request,
    transport_errors,
)
from app.ai.llm.errors import LLMUnavailableError, ModelNotAvailableError
from app.ai.llm.types import ChatMessage, GenerationOptions, LLMResult, Usage

# Pulling a model downloads gigabytes; allow much longer than a normal request.
PULL_TIMEOUT = 3600.0


def normalize_model_name(name: str) -> str:
    """``bge-m3`` and ``bge-m3:latest`` name the same Ollama model."""
    return name if ":" in name else f"{name}:latest"


class OllamaProvider:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 300.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client = client or build_client(base_url, timeout=timeout)

    @staticmethod
    def _options(options: GenerationOptions | None) -> dict[str, Any]:
        if options is None:
            return {}
        result: dict[str, Any] = {}
        if options.temperature is not None:
            result["temperature"] = options.temperature
        if options.max_tokens is not None:
            result["num_predict"] = options.max_tokens
        if options.context_tokens is not None:
            # Ollama's default context is small and silently truncates long prompts.
            result["num_ctx"] = options.context_tokens
        return result

    def _payload(
        self,
        messages: Sequence[ChatMessage],
        model: str,
        options: GenerationOptions | None,
        *,
        stream: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [m.model_dump() for m in messages],
            "stream": stream,
        }
        if opts := self._options(options):
            payload["options"] = opts
        return payload

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str,
        schema: type[BaseModel] | None = None,
        options: GenerationOptions | None = None,
    ) -> LLMResult:
        payload = self._payload(messages, model, options, stream=False)
        if schema is not None:
            payload["format"] = schema.model_json_schema()
        with transport_errors():
            response = await self._client.post("api/chat", json=payload)
        raise_for_status(response, model=model)
        data = json_object(response)
        message = data.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise LLMUnavailableError("Ollama returned an unexpected response")
        return LLMResult(
            content=message["content"],
            model=str(data.get("model", model)),
            usage=Usage(
                prompt_tokens=data.get("prompt_eval_count"),
                completion_tokens=data.get("eval_count"),
            ),
            finish_reason=data.get("done_reason"),
        )

    async def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str,
        options: GenerationOptions | None = None,
    ) -> AsyncIterator[str]:
        payload = self._payload(messages, model, options, stream=True)
        async with stream_request(self._client, "api/chat", payload, model=model) as response:
            with transport_errors():
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except ValueError as exc:
                        raise LLMUnavailableError("Ollama returned an invalid stream") from exc
                    if chunk.get("error"):
                        raise LLMUnavailableError("Ollama reported an error while streaming")
                    content = (chunk.get("message") or {}).get("content")
                    if content:
                        yield content
                    if chunk.get("done"):
                        return

    async def embed(self, texts: Sequence[str], *, model: str) -> list[list[float]]:
        if not texts:
            return []
        with transport_errors():
            response = await self._client.post(
                "api/embed", json={"model": model, "input": list(texts)}
            )
        raise_for_status(response, model=model)
        embeddings = json_object(response).get("embeddings")
        if not isinstance(embeddings, list) or len(embeddings) != len(texts):
            raise LLMUnavailableError("Ollama returned an unexpected number of embeddings")
        return embeddings

    async def list_models(self) -> list[str]:
        with transport_errors():
            response = await self._client.get("api/tags")
        raise_for_status(response)
        models = json_object(response).get("models") or []
        return [normalize_model_name(str(m.get("name") or m.get("model"))) for m in models]

    async def pull(self, model: str) -> None:
        """Download a model (blocks until done)."""
        with transport_errors():
            response = await self._client.post(
                "api/pull", json={"model": model, "stream": False}, timeout=PULL_TIMEOUT
            )
        raise_for_status(response, model=model)

    async def pull_progress(self, model: str) -> AsyncIterator[tuple[int, int]]:
        """Download a model, yielding ``(completed, total)`` bytes over all layers seen so
        far. Ends once Ollama reports success."""
        layers: dict[str, tuple[int, int]] = {}
        payload = {"model": model, "stream": True}
        # The read timeout applies between progress lines, not to the whole download.
        async with stream_request(self._client, "api/pull", payload, model=model) as response:
            with transport_errors():
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except ValueError as exc:
                        raise LLMUnavailableError("Ollama returned an invalid stream") from exc
                    if chunk.get("error"):
                        # E.g. an unknown model; the text is not passed on.
                        raise ModelNotAvailableError(model)
                    digest, total = chunk.get("digest"), chunk.get("total")
                    if isinstance(digest, str) and isinstance(total, int):
                        completed = chunk.get("completed")
                        layers[digest] = (completed if isinstance(completed, int) else 0, total)
                        yield (
                            sum(done for done, _ in layers.values()),
                            sum(size for _, size in layers.values()),
                        )
                    if chunk.get("status") == "success":
                        return
        raise LLMUnavailableError("Ollama ended the pull without success")

    async def aclose(self) -> None:
        await self._client.aclose()
