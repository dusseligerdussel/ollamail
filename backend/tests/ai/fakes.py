"""In-memory provider for gateway and structured-output tests."""

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel

from app.ai.llm.config import EndpointConfig
from app.ai.llm.metrics import LLMCallMetrics
from app.ai.llm.types import ChatMessage, GenerationOptions, LLMResult, Usage


@dataclass
class Call:
    messages: list[ChatMessage]
    model: str
    schema: type[BaseModel] | None
    options: GenerationOptions | None


@dataclass
class FakeProvider:
    """Returns scripted answers in order; an ``Exception`` in the script is raised."""

    answers: list[str | Exception] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)
    closed: bool = False
    pulled: list[str] = field(default_factory=list)

    def _next(self) -> str:
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str,
        schema: type[BaseModel] | None = None,
        options: GenerationOptions | None = None,
    ) -> LLMResult:
        self.calls.append(Call(list(messages), model, schema, options))
        content = self._next()
        return LLMResult(content=content, model=model, usage=Usage(10, 5))

    async def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str,
        options: GenerationOptions | None = None,
    ) -> AsyncIterator[str]:
        self.calls.append(Call(list(messages), model, None, options))
        for word in self._next().split(" "):
            yield word + " "

    async def embed(self, texts: Sequence[str], *, model: str) -> list[list[float]]:
        return [[float(len(t)), 0.0] for t in texts]

    async def list_models(self) -> list[str]:
        return list(self.models)

    async def aclose(self) -> None:
        self.closed = True


class FakeFactory:
    """Provider factory that hands out (and remembers) one fake per endpoint."""

    def __init__(self, **providers: FakeProvider) -> None:
        self.providers = providers
        self.created: list[EndpointConfig] = []

    def __call__(self, endpoint: EndpointConfig) -> FakeProvider:
        self.created.append(endpoint)
        return self.providers.setdefault(endpoint.name, FakeProvider())


class RecordingSink:
    def __init__(self) -> None:
        self.records: list[LLMCallMetrics] = []

    def record(self, metrics: LLMCallMetrics) -> None:
        self.records.append(metrics)
