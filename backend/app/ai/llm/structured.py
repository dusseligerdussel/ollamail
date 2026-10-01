"""Structured output: Pydantic schema -> JSON schema -> validated object.

Native mode passes the schema to the endpoint (Ollama ``format``, OpenAI
``response_format``); prompt mode relies on the instructions alone. Both add the schema
to the system prompt, which helps small models. Invalid output triggers a retry with a
correction hint, then :class:`LLMOutputError`.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ValidationError

from app.ai.llm.base import LLMProvider
from app.ai.llm.errors import LLMOutputError
from app.ai.llm.types import ChatMessage, GenerationOptions, LLMResult, Usage
from app.ai.prompts.structured_output import STRUCTURED_OUTPUT, STRUCTURED_OUTPUT_RETRY

# Validation details sent back to the model, at most this many.
MAX_REPORTED_ERRORS = 5


@dataclass(frozen=True, slots=True)
class StructuredResult[T: BaseModel]:
    value: T
    attempts: int
    usage: Usage
    model: str


def extract_json(text: str) -> str:
    """The JSON object in ``text``: tolerates Markdown fences and surrounding prose."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`").removeprefix("json").strip()
    start, end = stripped.find("{"), stripped.rfind("}")
    if start == -1 or end < start:
        return stripped
    return stripped[start : end + 1]


def describe_errors(exc: ValidationError) -> str:
    """Field locations and error types only; never the rejected values."""
    parts = []
    for error in exc.errors(include_input=False, include_url=False)[:MAX_REPORTED_ERRORS]:
        location = ".".join(str(p) for p in error["loc"]) or "root"
        parts.append(f"{location}: {error['msg']}")
    return "; ".join(parts)


def with_schema_instructions(
    messages: Sequence[ChatMessage], schema: type[BaseModel], language: str | None
) -> list[ChatMessage]:
    schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
    (instruction,) = STRUCTURED_OUTPUT.render(language, schema=schema_json)
    result = list(messages)
    if result and result[0].role == "system":
        merged = f"{result[0].content}\n\n{instruction.content}"
        result[0] = result[0].model_copy(update={"content": merged})
    else:
        result.insert(0, instruction)
    return result


def _add_usage(total: Usage, result: LLMResult) -> Usage:
    def add(a: int | None, b: int | None) -> int | None:
        return None if a is None and b is None else (a or 0) + (b or 0)

    return Usage(
        prompt_tokens=add(total.prompt_tokens, result.usage.prompt_tokens),
        completion_tokens=add(total.completion_tokens, result.usage.completion_tokens),
    )


async def complete_structured[T: BaseModel](
    provider: LLMProvider,
    messages: Sequence[ChatMessage],
    schema: type[T],
    *,
    model: str,
    mode: Literal["native", "prompt"],
    retries: int,
    options: GenerationOptions | None = None,
    language: str | None = None,
) -> StructuredResult[T]:
    conversation = with_schema_instructions(messages, schema, language)
    usage = Usage()
    for attempt in range(1, retries + 2):
        result = await provider.complete(
            conversation,
            model=model,
            schema=schema if mode == "native" else None,
            options=options,
        )
        usage = _add_usage(usage, result)
        try:
            value = schema.model_validate_json(extract_json(result.content))
        except ValidationError as exc:
            (hint,) = STRUCTURED_OUTPUT_RETRY.render(language, errors=describe_errors(exc))
            conversation = [
                *conversation,
                ChatMessage(role="assistant", content=result.content),
                ChatMessage(role="user", content=hint.content),
            ]
            continue
        return StructuredResult(value=value, attempts=attempt, usage=usage, model=result.model)
    raise LLMOutputError(schema.__name__, attempts=retries + 1)
