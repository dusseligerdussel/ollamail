"""OpenAI-compatible Chat Completions API (vLLM, LM Studio, LocalAI, llama.cpp server,
OpenAI, Azure/Anthropic gateways). ``base_url`` is the API root including ``/v1``."""

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
from app.ai.llm.errors import (
    LLMRequestError,
    LLMUnavailableError,
    StructuredOutputUnsupportedError,
)
from app.ai.llm.types import ChatMessage, GenerationOptions, LLMResult, Usage


class OpenAICompatibleProvider:
    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        timeout: float = 300.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
        self._client = client or build_client(base_url, timeout=timeout, headers=headers)

    @staticmethod
    def _payload(
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
        if options is not None:
            if options.temperature is not None:
                payload["temperature"] = options.temperature
            if options.max_tokens is not None:
                payload["max_tokens"] = options.max_tokens
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
            payload["response_format"] = {
                "type": "json_schema",
                # Not "strict": strict mode rejects common Pydantic schemas (defaults, $ref).
                "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema()},
            }
        with transport_errors():
            response = await self._client.post("chat/completions", json=payload)
        try:
            raise_for_status(response, model=model)
        except LLMRequestError as exc:
            if schema is not None and exc.status_code in {400, 422}:
                raise StructuredOutputUnsupportedError(
                    str(exc), status_code=exc.status_code
                ) from exc
            raise
        data = json_object(response)
        try:
            choice = data["choices"][0]
            content = choice["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMUnavailableError("endpoint returned an unexpected response") from exc
        usage = data.get("usage") or {}
        return LLMResult(
            content=content,
            model=str(data.get("model", model)),
            usage=Usage(
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
            ),
            finish_reason=choice.get("finish_reason"),
        )

    async def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str,
        options: GenerationOptions | None = None,
    ) -> AsyncIterator[str]:
        payload = self._payload(messages, model, options, stream=True)
        async with stream_request(self._client, "chat/completions", payload, model=model) as r:
            with transport_errors():
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line.removeprefix("data:").strip()
                    if data == "[DONE]":
                        return
                    try:
                        chunk = json.loads(data)
                    except ValueError as exc:
                        raise LLMUnavailableError("endpoint returned an invalid stream") from exc
                    for choice in chunk.get("choices") or []:
                        content = (choice.get("delta") or {}).get("content")
                        if content:
                            yield content

    async def embed(self, texts: Sequence[str], *, model: str) -> list[list[float]]:
        if not texts:
            return []
        with transport_errors():
            response = await self._client.post(
                "embeddings", json={"model": model, "input": list(texts)}
            )
        raise_for_status(response, model=model)
        items = json_object(response).get("data")
        if not isinstance(items, list) or len(items) != len(texts):
            raise LLMUnavailableError("endpoint returned an unexpected number of embeddings")
        ordered = sorted(items, key=lambda item: int(item.get("index", 0)))
        return [item["embedding"] for item in ordered]

    async def list_models(self) -> list[str]:
        with transport_errors():
            response = await self._client.get("models")
        raise_for_status(response)
        return [str(m["id"]) for m in json_object(response).get("data") or [] if "id" in m]

    async def aclose(self) -> None:
        await self._client.aclose()
