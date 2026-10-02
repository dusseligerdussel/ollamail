"""Run the evaluation for one or more models and build the report.

Each model gets its own ``LLMGateway`` (``EnvConfigResolver``: environment plus the
overrides of the command line) and serves all chat tasks (triage, todos, digest, RAG).
The embedding model is the same for all models, so the RAG index is built once.
"""

import os
import platform
import sys
import time
from collections.abc import Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from app.ai.llm import EnvConfigResolver, LLMGateway, LLMTask
from app.ai.llm.gateway import ProviderFactory, create_provider
from app.ai.prompts import registry
from app.core.config import LLMSettings, Settings
from app.evals.dataset import Dataset
from app.evals.digest import run_digest
from app.evals.metrics import RecordingSink, call_stats
from app.evals.rag import EvalInbox, eval_inbox, run_rag
from app.evals.report import STAGES, ModelResult, Report
from app.evals.todos import run_todos
from app.evals.triage import run_triage
from app.search.embedder import GatewayEmbedder

CHAT_TASKS = (LLMTask.TRIAGE, LLMTask.TODOS, LLMTask.DIGEST, LLMTask.RAG_CHAT)


@dataclass
class RunOptions:
    models: Sequence[str | None]
    stages: Sequence[str] = STAGES
    base_url: str | None = None
    provider: str | None = None
    embedding_model: str | None = None
    judge_model: str | None = None
    database_url: str | None = None
    use_prefilter: bool = True
    settings: Settings = field(default_factory=Settings)
    provider_factory: ProviderFactory | None = None
    # Deadline per model call for every chat task (``OLLAMAIL_LLM_CALL_TIMEOUT``);
    # ``None``: as configured (environment, else the profile's per-task defaults).
    timeout: float | None = None


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def llm_settings(options: RunOptions, model: str | None) -> LLMSettings:
    """Environment settings with the command-line overrides; ``model`` serves every chat
    task (task overrides from the environment would otherwise win)."""
    update: dict[str, Any] = {}
    if options.base_url:
        update["base_url"] = options.base_url
    if options.provider:
        update["provider"] = options.provider
    if options.embedding_model:
        update["task_embeddings_model"] = options.embedding_model
    if options.timeout is not None:
        # Task settings from the environment would otherwise win.
        update["call_timeout"] = options.timeout
        update |= {f"task_{task.value}_call_timeout": options.timeout for task in CHAT_TASKS}
        # The HTTP read timeout must not fire before the deadline.
        update["timeout"] = max(options.settings.llm.timeout, options.timeout + 30)
    if model:
        update["default_chat_model"] = model
        update |= {f"task_{task.value}_model": model for task in CHAT_TASKS}
    return LLMSettings.model_validate(options.settings.llm.model_dump() | update)


def gateway(settings: LLMSettings, options: RunOptions, sink: RecordingSink) -> LLMGateway:
    return LLMGateway(
        EnvConfigResolver(settings),
        provider_factory=options.provider_factory or create_provider,
        metrics=sink,
    )


def _safe_url(url: str) -> str:
    """URL without user info (credentials never go into a report)."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def _cpu_model() -> str | None:
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as cpuinfo:
            for line in cpuinfo:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        return None
    return None


def host_info() -> dict[str, Any]:
    cpu = _cpu_model() or platform.processor() or platform.machine()
    cpus = os.cpu_count()
    return {
        "system": platform.system(),
        "machine": platform.machine(),
        "cpu": cpu,
        "cpu_count": cpus,
        "description": f"{cpu}, {cpus} logical CPUs, {platform.system()} {platform.machine()}",
    }


def prompt_versions() -> dict[str, str]:
    names = (
        "triage",
        "todos_extract",
        "digest_map",
        "digest_condense",
        "digest_reduce",
        "rag_query",
        "rag_rerank",
        "rag_answer",
        "eval_judge",
    )
    return {name: registry.get(name).id for name in names}


async def _evaluate_model(
    model: str | None,
    dataset: Dataset,
    options: RunOptions,
    inbox: EvalInbox | None,
    judge: LLMGateway | None,
) -> ModelResult:
    sink = RecordingSink()
    settings = llm_settings(options, model)
    llm = gateway(settings, options, sink)
    name = model or await llm.assigned_model(LLMTask.TRIAGE)
    result = ModelResult(model=name)
    started = time.perf_counter()
    try:
        if "triage" in options.stages:
            _log(f"[{name}] triage: {len(dataset.mails)} mails")
            triage = await run_triage(
                llm,
                dataset.mails,
                use_prefilter=options.use_prefilter,
                settings=options.settings.triage,
            )
            result.stages["triage"] = triage.as_dict()
        if "todos" in options.stages:
            _log(f"[{name}] todos")
            todos = await run_todos(llm, dataset.mails, settings=options.settings.todos)
            result.stages["todos"] = todos.as_dict()
        if "digest" in options.stages:
            _log(f"[{name}] digest")
            digest = await run_digest(llm, dataset.mails, settings=options.settings.digest)
            result.stages["digest"] = digest.as_dict()
        if "rag" in options.stages and inbox is not None:
            _log(f"[{name}] rag: {len(dataset.questions)} questions")
            rag_settings = options.settings.model_copy(update={"llm": settings})
            rag = await run_rag(llm, inbox, dataset.questions, rag_settings, judge=judge)
            result.stages["rag"] = rag.as_dict()
    except Exception as exc:  # one broken model must not end the run
        result.error = type(exc).__name__
        _log(f"[{name}] failed: {type(exc).__name__}")
    finally:
        await llm.aclose()
    result.seconds = time.perf_counter() - started
    all_calls = []
    for task in CHAT_TASKS:
        calls = sink.for_task(task.value)
        all_calls += calls
        if calls:
            result.calls[task.value] = call_stats(calls).as_dict()
    timed_out = call_stats(all_calls).timeouts
    result.timeouts = {
        "calls": len(all_calls),
        "timed_out": timed_out,
        "rate": round(timed_out / len(all_calls), 4) if all_calls else None,
    }
    return result


async def run(dataset: Dataset, options: RunOptions) -> Report:
    started_at = datetime.now(UTC)
    stages = [s for s in STAGES if s in options.stages]
    if "rag" in stages and not options.database_url:
        _log("rag: skipped, no database (--database-url or OLLAMAIL_EVAL_DATABASE_URL)")
        stages.remove("rag")
    options.stages = stages
    base = llm_settings(options, None)
    resolver = EnvConfigResolver(base)
    deadlines = {task.value: (await resolver.resolve(task)).call_timeout for task in CHAT_TASKS}
    run_info: dict[str, Any] = {
        "started_at": started_at.isoformat(timespec="seconds"),
        "host": host_info(),
        "endpoint": {"provider": base.provider, "base_url": _safe_url(base.base_url)},
        "profile": base.profile,
        "stages": stages,
        "dataset": {
            "mails": len(dataset.mails),
            "questions": len(dataset.questions),
            "languages": sorted({m.language for m in dataset.mails}),
        },
        "prefilter": options.use_prefilter,
        "call_timeouts": deadlines,
        "judge_model": options.judge_model,
        "prompt_versions": prompt_versions(),
    }
    results: list[ModelResult] = []
    async with AsyncExitStack() as stack:
        inbox: EvalInbox | None = None
        judge: LLMGateway | None = None
        if "rag" in stages:
            assert options.database_url is not None
            index_sink = RecordingSink()
            indexer = gateway(base, options, index_sink)
            stack.push_async_callback(indexer.aclose)
            embedder = GatewayEmbedder(indexer, options.settings.search)
            run_info["embedding_model"] = await embedder.current_model()
            _log(f"rag: indexing {len(dataset.mails)} mails with {run_info['embedding_model']}")
            inbox, index_seconds = await stack.enter_async_context(
                eval_inbox(options.database_url, dataset, embedder, options.settings)
            )
            run_info["index_seconds"] = round(index_seconds, 1)
            embed_calls = index_sink.for_task(LLMTask.EMBEDDINGS.value)
            run_info["embedding_calls"] = call_stats(embed_calls).as_dict()
        if options.judge_model:
            judge = gateway(llm_settings(options, options.judge_model), options, RecordingSink())
            stack.push_async_callback(judge.aclose)
        for model in options.models:
            result = await _evaluate_model(model, dataset, options, inbox, judge)
            if "rag" in result.stages:
                result.stages["rag"]["embedding_model"] = run_info.get("embedding_model")
                result.stages["rag"]["indexed_mails"] = len(dataset.mails)
                result.stages["rag"]["index_seconds"] = run_info.get("index_seconds")
            results.append(result)
    run_info["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    return Report(run=run_info, models=results)
