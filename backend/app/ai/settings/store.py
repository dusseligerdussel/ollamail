"""Reading the stored AI settings and announcing changes to all processes."""

from typing import Any, cast, get_args

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.llm.config import AIOverrides, EndpointConfig, TaskOverride
from app.ai.llm.types import LLMTask
from app.ai.settings.models import AIProviderRecord, AISettingsRecord
from app.core.config import LLMProfileName, LLMProviderKind, StructuredOutputMode

# NOTIFY channel; the payload is empty (listeners reload everything).
CHANNEL = "ollamail_ai_settings"


def endpoint_config(record: AIProviderRecord, default_timeout: float) -> EndpointConfig:
    return EndpointConfig(
        name=record.name,
        provider=cast(LLMProviderKind, record.kind),
        base_url=record.base_url,
        api_key=record.api_key or None,
        is_cloud=record.is_cloud,
        structured_output=cast(StructuredOutputMode, record.structured_output),
        timeout=record.timeout or default_timeout,
    )


def task_overrides(tasks: dict[str, Any]) -> dict[LLMTask, TaskOverride]:
    """Stored task assignments; unknown tasks and malformed entries are ignored."""
    result: dict[LLMTask, TaskOverride] = {}
    for task in LLMTask:
        value = tasks.get(task.value)
        if not isinstance(value, dict):
            continue
        endpoint, model = value.get("provider"), value.get("model")
        result[task] = TaskOverride(
            endpoint=endpoint if isinstance(endpoint, str) and endpoint else None,
            model=model if isinstance(model, str) and model else None,
        )
    return result


async def get_provider(session: AsyncSession, name: str) -> AIProviderRecord | None:
    return await session.scalar(select(AIProviderRecord).where(AIProviderRecord.name == name))


async def get_settings_record(session: AsyncSession) -> AISettingsRecord | None:
    return await session.scalar(select(AISettingsRecord))


async def list_providers(session: AsyncSession) -> list[AIProviderRecord]:
    return list(await session.scalars(select(AIProviderRecord).order_by(AIProviderRecord.name)))


async def load_overrides(session: AsyncSession, default_timeout: float) -> AIOverrides:
    """Everything the resolver needs, with API keys decrypted (kept in memory only)."""
    providers = await list_providers(session)
    record = await get_settings_record(session)
    endpoints = {p.name: endpoint_config(p, default_timeout) for p in providers}
    if record is None:
        return AIOverrides(endpoints=endpoints)
    profile = record.profile if record.profile in get_args(LLMProfileName) else None
    return AIOverrides(
        endpoints=endpoints,
        cloud_enabled=record.cloud_enabled,
        profile=cast(LLMProfileName | None, profile),
        concurrency=record.concurrency,
        tasks=task_overrides(record.tasks or {}),
    )


async def notify_changed(session: AsyncSession) -> None:
    """Tell every API and worker process to reload; delivered on commit."""
    await session.execute(text("SELECT pg_notify(:channel, '')"), {"channel": CHANNEL})
