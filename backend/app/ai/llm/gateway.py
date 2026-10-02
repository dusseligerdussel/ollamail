"""Entry point for features: ``await llm.complete_structured(LLMTask.TRIAGE, ...)``.

The gateway resolves endpoint and model per task, enforces the cloud switch, fits
prompts into the context window, bounds answer length and call duration, validates
structured output and records metrics. Features never talk to a provider directly.
"""

import asyncio
import dataclasses
import math
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Sequence
from contextlib import (
    AbstractAsyncContextManager,
    aclosing,
    asynccontextmanager,
    nullcontext,
)
from typing import Literal

from fastapi import Request
from pydantic import BaseModel

from app.ai.llm.base import LLMProvider
from app.ai.llm.config import EndpointConfig, LLMConfigResolver, ModelAssignment
from app.ai.llm.context import CHARS_PER_TOKEN, estimate_tokens, fit_messages
from app.ai.llm.errors import (
    CloudLLMDisabledError,
    LLMError,
    LLMNotReadyError,
    LLMRequestError,
    LLMTimeoutError,
    LLMUnavailableError,
    StructuredOutputUnsupportedError,
)
from app.ai.llm.limiter import DynamicLimiter
from app.ai.llm.metrics import LLMCallMetrics, LoggingMetricsSink, MetricsSink, Operation
from app.ai.llm.ollama import OllamaProvider, normalize_model_name
from app.ai.llm.openai_compat import OpenAICompatibleProvider
from app.ai.llm.structured import StructuredResult, complete_structured
from app.ai.llm.types import ChatMessage, GenerationOptions, LLMResult, LLMTask, Usage
from app.core.config import LLMProviderKind
from app.core.logging import get_logger

ProviderFactory = Callable[[EndpointConfig], LLMProvider]

# ``disabled``: assigned to a cloud endpoint while cloud LLMs are switched off.
ModelState = Literal["installed", "missing", "unreachable", "disabled"]


@dataclasses.dataclass(frozen=True)
class ModelStatus:
    task: LLMTask
    endpoint: str
    provider: LLMProviderKind
    model: str
    state: ModelState


log = get_logger(__name__)


def create_provider(endpoint: EndpointConfig) -> LLMProvider:
    if endpoint.provider == "ollama":
        return OllamaProvider(endpoint.base_url, timeout=endpoint.timeout)
    return OpenAICompatibleProvider(
        endpoint.base_url, api_key=endpoint.api_key, timeout=endpoint.timeout
    )


def _reserve_tokens(options: GenerationOptions | None, assignment: ModelAssignment) -> int:
    """Room left for the answer when fitting the prompt."""
    if options is not None and options.max_tokens is not None:
        return options.max_tokens
    return min(assignment.max_output_tokens, assignment.context_tokens // 4)


def _deadline_exceeded(seconds: float) -> LLMTimeoutError:
    # No prompt or output in the message (docs/PRIVACY.md).
    return LLMTimeoutError(f"LLM call exceeded its deadline of {seconds:g} s")


@asynccontextmanager
async def _deadline(seconds: float | None) -> AsyncIterator[None]:
    """Cancel the enclosed call after ``seconds`` and raise :class:`LLMTimeoutError`."""
    if seconds is None:
        yield
        return
    timeout = asyncio.timeout(seconds)
    try:
        async with timeout:
            yield
    except TimeoutError as exc:
        if timeout.expired():
            raise _deadline_exceeded(seconds) from exc
        raise


class LLMGateway:
    def __init__(
        self,
        resolver: LLMConfigResolver,
        *,
        provider_factory: ProviderFactory = create_provider,
        metrics: MetricsSink | None = None,
        concurrency: Callable[[], Awaitable[int]] | None = None,
    ) -> None:
        """``concurrency`` limits parallel requests of this gateway (worker: the admin
        setting, re-read on every request); ``None`` means no limit."""
        self._resolver = resolver
        self._limiter = DynamicLimiter(concurrency) if concurrency is not None else None
        self._provider_factory = provider_factory
        self._metrics = metrics or LoggingMetricsSink()
        self._providers: dict[str, tuple[EndpointConfig, LLMProvider]] = {}
        # (endpoint, model) pairs that rejected the native JSON-schema parameter.
        self._prompt_only: set[tuple[str, str]] = set()

    async def _provider(self, endpoint: EndpointConfig) -> LLMProvider:
        cached = self._providers.get(endpoint.name)
        if cached is not None:
            config, provider = cached
            if config == endpoint:
                return provider
            # Configuration changed at runtime (admin settings): replace the client.
            await provider.aclose()
        provider = self._provider_factory(endpoint)
        self._providers[endpoint.name] = (endpoint, provider)
        return provider

    def _slot(self) -> AbstractAsyncContextManager[None]:
        return self._limiter.slot() if self._limiter is not None else nullcontext()

    async def _select(self, task: LLMTask) -> tuple[ModelAssignment, LLMProvider]:
        assignment = await self._resolver.resolve(task)
        if assignment.endpoint.is_cloud and not await self._resolver.cloud_allowed():
            raise CloudLLMDisabledError(assignment.endpoint.name)
        return assignment, await self._provider(assignment.endpoint)

    def _record(
        self,
        assignment: ModelAssignment,
        operation: Operation,
        prompt_version: str | None,
        started: float,
        *,
        error: BaseException | None = None,
        usage: Usage | None = None,
        attempts: int = 1,
    ) -> None:
        usage = usage or Usage()
        self._metrics.record(
            LLMCallMetrics(
                task=assignment.task.value,
                operation=operation,
                endpoint=assignment.endpoint.name,
                provider=assignment.endpoint.provider,
                model=assignment.model,
                prompt_version=prompt_version,
                duration_ms=round((time.perf_counter() - started) * 1000),
                success=error is None,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                attempts=attempts,
                error_type=type(error).__name__ if error is not None else None,
            )
        )

    @staticmethod
    def _prepare(
        assignment: ModelAssignment,
        messages: Sequence[ChatMessage],
        options: GenerationOptions | None,
    ) -> tuple[list[ChatMessage], GenerationOptions]:
        context = assignment.context_tokens
        fitted = fit_messages(
            messages, context_tokens=context, reserve_tokens=_reserve_tokens(options, assignment)
        )
        base = options or GenerationOptions()
        merged = GenerationOptions(
            temperature=base.temperature,
            # Every call is bounded: without a limit a small model may generate until the
            # context window is full (#132).
            max_tokens=base.max_tokens or assignment.max_output_tokens,
            context_tokens=base.context_tokens or context,
        )
        return fitted, merged

    async def complete(
        self,
        task: LLMTask,
        messages: Sequence[ChatMessage],
        *,
        prompt_version: str | None = None,
        options: GenerationOptions | None = None,
    ) -> LLMResult:
        assignment, provider = await self._select(task)
        fitted, opts = self._prepare(assignment, messages, options)
        started = time.perf_counter()
        try:
            async with self._slot(), _deadline(assignment.call_timeout):
                result = await provider.complete(fitted, model=assignment.model, options=opts)
        except Exception as exc:
            self._record(assignment, "complete", prompt_version, started, error=exc)
            raise
        self._record(assignment, "complete", prompt_version, started, usage=result.usage)
        return result

    async def complete_structured[T: BaseModel](
        self,
        task: LLMTask,
        messages: Sequence[ChatMessage],
        schema: type[T],
        *,
        prompt_version: str | None = None,
        options: GenerationOptions | None = None,
        language: str | None = None,
    ) -> T:
        """Validated instance of ``schema``; raises :class:`LLMOutputError` if the model
        does not produce valid output within the configured retries. The call deadline
        covers all attempts."""
        assignment, provider = await self._select(task)
        fitted, opts = self._prepare(assignment, messages, options)
        retries = await self._resolver.structured_output_retries()
        key = (assignment.endpoint.name, assignment.model)
        native = assignment.endpoint.structured_output == "native" and key not in self._prompt_only
        started = time.perf_counter()
        try:
            async with self._slot(), _deadline(assignment.call_timeout):
                result = await self._structured(
                    provider, assignment, fitted, schema, opts, native, retries, language
                )
        except Exception as exc:
            attempts = getattr(exc, "attempts", 1)
            self._record(
                assignment, "structured", prompt_version, started, error=exc, attempts=attempts
            )
            raise
        self._record(
            assignment,
            "structured",
            prompt_version,
            started,
            usage=result.usage,
            attempts=result.attempts,
        )
        return result.value

    async def _structured[T: BaseModel](
        self,
        provider: LLMProvider,
        assignment: ModelAssignment,
        fitted: list[ChatMessage],
        schema: type[T],
        opts: GenerationOptions,
        native: bool,
        retries: int,
        language: str | None,
    ) -> StructuredResult[T]:
        key = (assignment.endpoint.name, assignment.model)
        try:
            return await complete_structured(
                provider,
                fitted,
                schema,
                model=assignment.model,
                mode="native" if native else "prompt",
                retries=retries,
                options=opts,
                language=language,
            )
        except StructuredOutputUnsupportedError:
            if not native:
                raise
            self._prompt_only.add(key)
            log.warning(
                "llm_structured_output_fallback",
                endpoint=assignment.endpoint.name,
                model=assignment.model,
            )
            return await complete_structured(
                provider,
                fitted,
                schema,
                model=assignment.model,
                mode="prompt",
                retries=retries,
                options=opts,
                language=language,
            )

    async def stream(
        self,
        task: LLMTask,
        messages: Sequence[ChatMessage],
        *,
        prompt_version: str | None = None,
        options: GenerationOptions | None = None,
    ) -> AsyncIterator[str]:
        assignment, provider = await self._select(task)
        fitted, opts = self._prepare(assignment, messages, options)
        started = time.perf_counter()
        streamed_chars = 0
        error: BaseException | None = None
        try:
            async with (
                self._slot(),
                aclosing(
                    self._bounded_stream(
                        provider.stream(fitted, model=assignment.model, options=opts),
                        assignment.call_timeout,
                    )
                ) as chunks,
            ):
                async for chunk in chunks:
                    streamed_chars += len(chunk)
                    yield chunk
        except BaseException as exc:
            error = exc
            raise
        finally:
            # Streams carry no usage data; the answer size is estimated.
            usage = Usage(completion_tokens=math.ceil(streamed_chars / CHARS_PER_TOKEN))
            self._record(assignment, "stream", prompt_version, started, error=error, usage=usage)

    @staticmethod
    async def _bounded_stream(
        chunks: AsyncIterator[str], seconds: float | None
    ) -> AsyncGenerator[str]:
        """``chunks`` until the deadline, then :class:`LLMTimeoutError`. Only the wait for
        the next chunk is timed out, so the consumer's own awaits between chunks are
        never cancelled; the time the consumer takes still counts towards the deadline."""
        deadline = None if seconds is None else asyncio.get_running_loop().time() + seconds
        try:
            while True:
                timeout = asyncio.timeout_at(deadline)
                try:
                    async with timeout:
                        chunk = await anext(chunks)
                except StopAsyncIteration:
                    return
                except TimeoutError as exc:
                    if seconds is None or not timeout.expired():
                        raise
                    raise _deadline_exceeded(seconds) from exc
                yield chunk
        finally:
            # Closes the HTTP stream of the provider.
            aclose = getattr(chunks, "aclose", None)
            if aclose is not None:
                await aclose()

    async def assignment(self, task: LLMTask) -> ModelAssignment:
        """Endpoint and model currently serving ``task`` (e.g. to tag stored embeddings)."""
        return await self._resolver.resolve(task)

    async def embed(
        self,
        texts: Sequence[str],
        *,
        task: LLMTask = LLMTask.EMBEDDINGS,
        model: str | None = None,
    ) -> list[list[float]]:
        """Embed ``texts``. ``model`` overrides the task's model on the task's endpoint,
        e.g. to query an index built with the previous model while it is rebuilt."""
        assignment, provider = await self._select(task)
        if model is not None:
            assignment = dataclasses.replace(assignment, model=model)
        started = time.perf_counter()
        try:
            async with self._slot():
                vectors = await provider.embed(texts, model=assignment.model)
        except Exception as exc:
            self._record(assignment, "embed", None, started, error=exc)
            raise
        usage = Usage(prompt_tokens=sum(estimate_tokens(t) for t in texts))
        self._record(assignment, "embed", None, started, usage=usage)
        return vectors

    async def assigned_model(self, task: LLMTask) -> str:
        """Model currently assigned to ``task`` (e.g. to tag stored embeddings)."""
        return (await self._resolver.resolve(task)).model

    async def _assignments(self) -> list[ModelAssignment]:
        return [await self._resolver.resolve(task) for task in LLMTask]

    @staticmethod
    def _same_model(endpoint: EndpointConfig, a: str, b: str) -> bool:
        if endpoint.provider == "ollama":
            return normalize_model_name(a) == normalize_model_name(b)
        return a == b

    async def check_ready(self) -> None:
        """Readiness check: every task's endpoint is reachable and serves its model."""
        cloud_allowed = await self._resolver.cloud_allowed()
        available: dict[str, list[str]] = {}
        for assignment in await self._assignments():
            endpoint = assignment.endpoint
            if endpoint.is_cloud and not cloud_allowed:
                raise LLMNotReadyError(f"task {assignment.task.value!r} uses a disabled endpoint")
            if endpoint.name not in available:
                provider = await self._provider(endpoint)
                available[endpoint.name] = await provider.list_models()
            if not any(
                self._same_model(endpoint, assignment.model, m) for m in available[endpoint.name]
            ):
                raise LLMNotReadyError(
                    f"model {assignment.model!r} is missing on endpoint {endpoint.name!r}"
                )

    async def model_status(self, *, wait: float = 5.0) -> list[ModelStatus]:
        """State of every task's model: installed, missing, endpoint unreachable, or
        assigned to a cloud endpoint while cloud LLMs are disabled. Each endpoint is asked
        once, for at most ``wait`` seconds."""
        cloud_allowed = await self._resolver.cloud_allowed()
        available: dict[str, list[str] | None] = {}
        result = []
        for assignment in await self._assignments():
            endpoint = assignment.endpoint
            state: ModelState
            if endpoint.is_cloud and not cloud_allowed:
                state = "disabled"
            else:
                if endpoint.name not in available:
                    provider = await self._provider(endpoint)
                    try:
                        async with asyncio.timeout(wait):
                            available[endpoint.name] = await provider.list_models()
                    except (LLMError, TimeoutError) as exc:
                        log.info(
                            "llm_model_status_failed",
                            endpoint=endpoint.name,
                            error_type=type(exc).__name__,
                        )
                        available[endpoint.name] = None
                models = available[endpoint.name]
                if models is None:
                    state = "unreachable"
                elif any(self._same_model(endpoint, assignment.model, m) for m in models):
                    state = "installed"
                else:
                    state = "missing"
            result.append(
                ModelStatus(
                    task=assignment.task,
                    endpoint=endpoint.name,
                    provider=endpoint.provider,
                    model=assignment.model,
                    state=state,
                )
            )
        return result

    async def pull_model(self, endpoint_name: str, model: str) -> AsyncIterator[tuple[int, int]]:
        """Download ``model`` on an Ollama endpoint that a task is assigned to, yielding
        ``(completed, total)`` bytes. Other endpoints raise :class:`LLMRequestError`."""
        for assignment in await self._assignments():
            endpoint = assignment.endpoint
            if endpoint.name == endpoint_name and self._same_model(
                endpoint, assignment.model, model
            ):
                break
        else:
            raise LLMRequestError(f"model {model!r} is not assigned on {endpoint_name!r}")
        provider = await self._provider(endpoint)
        if not isinstance(provider, OllamaProvider):
            raise LLMRequestError(f"endpoint {endpoint_name!r} cannot download models")
        async for progress in provider.pull_progress(assignment.model):
            yield progress

    async def pull_missing_models(self, *, attempts: int = 6, interval: float = 10.0) -> None:
        """Download models assigned to Ollama endpoints that are not installed yet. An
        unreachable endpoint is asked up to ``attempts`` times, ``interval`` seconds apart:
        the bundled Ollama container may start after the API."""
        pending: dict[str, tuple[EndpointConfig, set[str]]] = {}
        for assignment in await self._assignments():
            if assignment.endpoint.provider != "ollama":
                continue
            _, models = pending.setdefault(assignment.endpoint.name, (assignment.endpoint, set()))
            models.add(normalize_model_name(assignment.model))
        for name, (endpoint, models) in pending.items():
            provider = await self._provider(endpoint)
            if not isinstance(provider, OllamaProvider):
                continue
            installed = await self._installed(name, provider, attempts, interval)
            if installed is None:
                continue
            for model in sorted(models - installed):
                log.info("llm_model_pull_started", endpoint=name, model=model)
                try:
                    await provider.pull(model)
                except LLMError as exc:
                    log.warning(
                        "llm_model_pull_failed",
                        endpoint=name,
                        model=model,
                        error_type=type(exc).__name__,
                    )
                    continue
                log.info("llm_model_pull_finished", endpoint=name, model=model)

    @staticmethod
    async def _installed(
        name: str, provider: OllamaProvider, attempts: int, interval: float
    ) -> set[str] | None:
        for attempt in range(1, attempts + 1):
            try:
                return set(await provider.list_models())
            except LLMError as exc:
                if attempt == attempts or not isinstance(exc, LLMUnavailableError):
                    log.warning(
                        "llm_model_pull_failed", endpoint=name, error_type=type(exc).__name__
                    )
                    return None
            await asyncio.sleep(interval)
        return None

    async def aclose(self) -> None:
        for _, provider in self._providers.values():
            await provider.aclose()
        self._providers.clear()


def get_llm(request: Request) -> LLMGateway:
    """FastAPI dependency."""
    gateway: LLMGateway = request.app.state.llm
    return gateway
