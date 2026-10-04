"""Admin API for AI settings (``/admin/ai``) and the cloud notice for users (``/ai``).

Every change is audited (``ai.settings_changed``) and announced to all processes
(``store.notify_changed``), so it applies to the next LLM request without a restart.
"""

import dataclasses
import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.ai.llm.config import EndpointConfig, ResolvedConfig
from app.ai.llm.errors import LLMError, LLMRequestError, LLMUnavailableError
from app.ai.llm.gateway import create_provider
from app.ai.llm.profiles import PROFILES
from app.ai.llm.types import LLMTask
from app.ai.settings import store
from app.ai.settings.models import AIProviderRecord, AISettingsRecord
from app.ai.settings.resolver import DbConfigResolver
from app.ai.settings.schemas import (
    AIConnectionError,
    AIConnectionTest,
    AIProviderCreate,
    AIProviderRead,
    AIProviderTest,
    AIProviderUpdate,
    AISettingsRead,
    AISettingsUpdate,
    AIStatusRead,
    CloudUsage,
    ProfileRead,
    TaskSettingRead,
    display_url,
)
from app.auth.dependencies import AdminSessionDep, CurrentSessionDep, SettingsDep
from app.auth.reauth import ADMIN_REAUTH_RESPONSES, RecentAdminDep, check_recent
from app.core.config import LLMSettings
from app.core.db import get_db
from app.core.errors import ProblemError
from app.core.logging import get_logger

log = get_logger(__name__)

DbDep = Annotated[AsyncSession, Depends(get_db)]

router = APIRouter(
    prefix="/admin/ai",
    tags=["ai"],
    responses={401: {"description": "Not signed in"}, 403: {"description": "Not an admin"}},
)
status_router = APIRouter(prefix="/ai", tags=["ai"])

_NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"description": "No such provider"}}


async def _config(db: AsyncSession, settings: LLMSettings) -> ResolvedConfig:
    return ResolvedConfig(settings, await store.load_overrides(db, settings.timeout))


def _used_by(config: ResolvedConfig, name: str) -> list[LLMTask]:
    return [task for task in LLMTask if config.assignment(task).endpoint.name == name]


def _provider_read(
    config: ResolvedConfig, endpoint: EndpointConfig, record: AIProviderRecord | None
) -> AIProviderRead:
    return AIProviderRead(
        name=endpoint.name,
        display_name=record.display_name if record is not None else endpoint.name,
        kind=endpoint.provider,
        base_url=display_url(endpoint.base_url),
        api_key_set=bool(endpoint.api_key),
        is_cloud=endpoint.is_cloud,
        structured_output=endpoint.structured_output,
        timeout=record.timeout if record is not None else endpoint.timeout,
        source="database" if record is not None else "environment",
        used_by=_used_by(config, endpoint.name),
    )


async def _settings_read(db: AsyncSession, settings: LLMSettings) -> AISettingsRead:
    config = await _config(db, settings)
    env = ResolvedConfig(settings)
    tasks = []
    for task in LLMTask:
        stored = config.overrides.tasks.get(task)
        effective = config.assignment(task)
        # The same configuration without a stored choice for this task.
        without = ResolvedConfig(
            settings,
            dataclasses.replace(
                config.overrides,
                tasks={t: o for t, o in config.overrides.tasks.items() if t is not task},
            ),
        ).assignment(task)
        tasks.append(
            TaskSettingRead(
                task=task,
                provider=stored.endpoint if stored else None,
                model=stored.model if stored else None,
                effective_provider=effective.endpoint.name,
                effective_model=effective.model,
                default_provider=without.endpoint.name,
                default_model=without.model,
                blocked=effective.endpoint.is_cloud and not config.cloud_enabled,
            )
        )
    return AISettingsRead(
        cloud_enabled=config.cloud_enabled,
        cloud_enabled_default=env.cloud_enabled,
        profile=config.profile,
        profile_default=env.profile,
        profiles=[
            ProfileRead(
                name=name,
                chat_model=profile.chat_model,
                embedding_model=profile.embedding_model,
                context_tokens=profile.context_tokens,
            )
            for name, profile in PROFILES.items()
        ],
        context_tokens=config.context_tokens,
        concurrency=config.concurrency,
        concurrency_default=env.concurrency,
        concurrency_max=_max_concurrency(settings),
        tasks=tasks,
    )


def _max_concurrency(settings: LLMSettings) -> int:
    return max(settings.concurrency, settings.max_concurrency)


async def _changed(
    request: Request,
    db: AsyncSession,
    admin_id: Any,
    target: audit.Target,
    details: dict[str, Any],
) -> None:
    """Audit, announce and commit a change."""
    await audit.record(
        db, audit.Actor.user(admin_id), audit.AuditAction.AI_SETTINGS_CHANGED, target, details
    )
    await store.notify_changed(db)
    await db.commit()
    resolver: DbConfigResolver | None = getattr(request.app.state, "ai_resolver", None)
    if resolver is not None:
        resolver.invalidate()


def _settings_target() -> audit.Target:
    return audit.Target.of(audit.TargetType.SETTINGS, "ai")


async def _record(db: AsyncSession, name: str) -> AIProviderRecord:
    record = await store.get_provider(db, name)
    if record is None:
        raise ProblemError(404, detail="Provider not found.")
    return record


def _read_only() -> ProblemError:
    return ProblemError(
        409,
        detail="This provider is configured in the environment and cannot be changed here.",
        type="urn:ollamail:problem:ai-provider-read-only",
    )


@router.get("/settings")
async def get_ai_settings(_: AdminSessionDep, db: DbDep, settings: SettingsDep) -> AISettingsRead:
    """Effective AI settings with the environment's defaults."""
    return await _settings_read(db, settings.llm)


def _sends_mail_elsewhere(body: AISettingsUpdate, settings: LLMSettings) -> bool:
    """Whether the change can send mail contents to another endpoint: cloud providers
    turned on (also by a reset to an environment that allows them) or tasks reassigned."""
    if "cloud_enabled" in body.model_fields_set:
        enabled = settings.cloud_enabled if body.cloud_enabled is None else body.cloud_enabled
        if enabled:
            return True
    return bool(body.tasks)


@router.patch(
    "/settings",
    responses={**ADMIN_REAUTH_RESPONSES, 422: {"description": "Invalid settings"}},
)
async def update_ai_settings(
    body: AISettingsUpdate,
    request: Request,
    admin: AdminSessionDep,
    db: DbDep,
    settings: SettingsDep,
) -> AISettingsRead:
    """Change settings; ``null`` resets a field to the environment's value. Turning cloud
    providers on and assigning tasks need a recent confirmation (#206); turning them off,
    profile and concurrency do not."""
    if _sends_mail_elsewhere(body, settings.llm):
        check_recent(settings, admin)
    fields = body.model_fields_set
    config = await _config(db, settings.llm)
    if body.concurrency is not None and body.concurrency > _max_concurrency(settings.llm):
        raise ProblemError(
            422,
            detail=f"At most {_max_concurrency(settings.llm)} parallel requests are possible "
            "(OLLAMAIL_LLM_MAX_CONCURRENCY).",
        )
    for assignment in (body.tasks or {}).values():
        if assignment.provider is not None and assignment.provider not in config.endpoints:
            raise ProblemError(422, detail="Unknown provider.")
    record = await store.get_settings_record(db)
    if record is None:
        record = AISettingsRecord(tasks={})
        db.add(record)
    details: dict[str, Any] = {"change": "settings_updated"}
    if "cloud_enabled" in fields:
        record.cloud_enabled = body.cloud_enabled
        details["cloud_enabled"] = (
            settings.llm.cloud_enabled if body.cloud_enabled is None else body.cloud_enabled
        )
    if "profile" in fields:
        record.profile = body.profile
        details["profile"] = body.profile or settings.llm.profile
    if "concurrency" in fields:
        record.concurrency = body.concurrency
        details["concurrency"] = body.concurrency or settings.llm.concurrency
    if body.tasks:
        tasks = dict(record.tasks or {})
        for task, assignment in body.tasks.items():
            if assignment.provider is None and assignment.model is None:
                tasks.pop(task.value, None)
            else:
                tasks[task.value] = {"provider": assignment.provider, "model": assignment.model}
        # A new dict, so SQLAlchemy sees the change.
        record.tasks = tasks
        details["tasks"] = ",".join(sorted(task.value for task in body.tasks))
    await _changed(request, db, admin.user_id, _settings_target(), details)
    log.info("ai_settings_changed", change="settings_updated")
    return await _settings_read(db, settings.llm)


@router.get("/providers")
async def list_ai_providers(
    _: AdminSessionDep, db: DbDep, settings: SettingsDep
) -> list[AIProviderRead]:
    """Providers from the environment (read-only), then those added here, by name."""
    config = await _config(db, settings.llm)
    records = {r.name: r for r in await store.list_providers(db)}
    env = [_provider_read(config, e, None) for e in config.env_endpoints.values()]
    stored = [
        _provider_read(config, config.endpoints[name], record)
        for name, record in records.items()
        if name not in config.env_endpoints
    ]
    return env + stored


@router.post(
    "/providers",
    status_code=status.HTTP_201_CREATED,
    responses={
        **ADMIN_REAUTH_RESPONSES,
        409: {"description": "Name taken"},
        422: {"description": "Invalid settings"},
    },
)
async def create_ai_provider(
    body: AIProviderCreate,
    request: Request,
    admin: RecentAdminDep,
    db: DbDep,
    settings: SettingsDep,
) -> AIProviderRead:
    """Add a provider. Test it with ``…/test`` before assigning it to tasks."""
    config = await _config(db, settings.llm)
    if body.name in config.endpoints:
        raise ProblemError(
            409,
            detail="A provider with this name already exists.",
            type="urn:ollamail:problem:ai-provider-name-taken",
        )
    record = AIProviderRecord(
        name=body.name,
        display_name=body.display_name,
        kind=body.kind,
        base_url=body.base_url,
        api_key=body.api_key.get_secret_value() if body.api_key else None,
        is_cloud=body.is_cloud,
        structured_output=body.structured_output,
        timeout=body.timeout,
    )
    db.add(record)
    await db.flush()
    await _changed(
        request,
        db,
        admin.user_id,
        audit.Target.of(audit.TargetType.SETTINGS, record.id),
        {"change": "provider_created", "provider": record.name, "is_cloud": record.is_cloud},
    )
    config = await _config(db, settings.llm)
    return _provider_read(config, config.endpoints[record.name], record)


@router.patch(
    "/providers/{name}",
    responses={
        **ADMIN_REAUTH_RESPONSES,
        **_NOT_FOUND,
        409: {"description": "Provider from the environment"},
    },
)
async def update_ai_provider(
    name: str,
    body: AIProviderUpdate,
    request: Request,
    admin: RecentAdminDep,
    db: DbDep,
    settings: SettingsDep,
) -> AIProviderRead:
    """Change a provider. Omitted fields stay; ``api_key: null`` removes the key."""
    if name in ResolvedConfig(settings.llm).env_endpoints:
        raise _read_only()
    record = await _record(db, name)
    fields = body.model_fields_set
    for field in ("display_name", "kind", "base_url", "is_cloud", "structured_output"):
        value = getattr(body, field)
        if field in fields and value is not None:
            setattr(record, field, value)
    if "timeout" in fields:
        record.timeout = body.timeout
    if "api_key" in fields:
        record.api_key = body.api_key.get_secret_value() if body.api_key else None
    await _changed(
        request,
        db,
        admin.user_id,
        audit.Target.of(audit.TargetType.SETTINGS, record.id),
        {
            "change": "provider_updated",
            "provider": record.name,
            "is_cloud": record.is_cloud,
            "api_key_changed": "api_key" in fields,
        },
    )
    config = await _config(db, settings.llm)
    return _provider_read(config, config.endpoints[record.name], record)


@router.delete(
    "/providers/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        **_NOT_FOUND,
        409: {"description": "Provider from the environment or assigned to a task"},
    },
)
async def delete_ai_provider(
    name: str, request: Request, admin: AdminSessionDep, db: DbDep, settings: SettingsDep
) -> Response:
    """Remove a provider and its API key. Reassign its tasks first."""
    config = await _config(db, settings.llm)
    if name in config.env_endpoints:
        raise _read_only()
    record = await _record(db, name)
    stored_tasks = [t for t, o in config.overrides.tasks.items() if o.endpoint == name]
    if stored_tasks:
        raise ProblemError(
            409,
            detail="The provider is assigned to tasks. Assign them to another provider first.",
            type="urn:ollamail:problem:ai-provider-in-use",
        )
    await db.delete(record)
    await _changed(
        request,
        db,
        admin.user_id,
        audit.Target.of(audit.TargetType.SETTINGS, record.id),
        {"change": "provider_deleted", "provider": name},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _test(endpoint: EndpointConfig) -> AIConnectionTest:
    """List the endpoint's models. Sends no mail content, only the API key."""
    started = time.perf_counter()
    provider = create_provider(endpoint)
    error: AIConnectionError | None = None
    status_code: int | None = None
    models: list[str] = []
    try:
        models = sorted(await provider.list_models())
    except LLMUnavailableError:
        error = "unreachable"
    except LLMRequestError as exc:
        status_code = exc.status_code
        error = "unauthorized" if exc.status_code in {401, 403} else "rejected"
    except LLMError:
        error = "failed"
    finally:
        await provider.aclose()
    duration_ms = round((time.perf_counter() - started) * 1000)
    log.info(
        "ai_provider_tested",
        provider=endpoint.name,
        kind=endpoint.provider,
        ok=error is None,
        error=error,
        duration_ms=duration_ms,
    )
    return AIConnectionTest(
        ok=error is None,
        models=models,
        error=error,
        status_code=status_code,
        duration_ms=duration_ms,
    )


@router.post("/providers/test")
async def check_ai_provider_settings(
    body: AIProviderTest, _: AdminSessionDep, db: DbDep, settings: SettingsDep
) -> AIConnectionTest:
    """Test unsaved settings (e.g. in the form before saving)."""
    api_key = body.api_key.get_secret_value() if body.api_key else None
    if api_key is None and body.name is not None:
        config = await _config(db, settings.llm)
        stored = config.overrides.endpoints.get(body.name)
        api_key = stored.api_key if stored is not None else None
    return await _test(
        EndpointConfig(
            name=body.name or "test",
            provider=body.kind,
            base_url=body.base_url,
            api_key=api_key,
            timeout=body.timeout or min(settings.llm.timeout, 30.0),
        )
    )


@router.post("/providers/{name}/test", responses=_NOT_FOUND)
async def check_ai_provider(
    name: str, _: AdminSessionDep, db: DbDep, settings: SettingsDep
) -> AIConnectionTest:
    """Connect to a configured provider and list its models."""
    config = await _config(db, settings.llm)
    endpoint = config.endpoints.get(name)
    if endpoint is None:
        raise ProblemError(404, detail="Provider not found.")
    return await _test(endpoint)


@status_router.get("/status")
async def get_ai_status(_: CurrentSessionDep, db: DbDep, settings: SettingsDep) -> AIStatusRead:
    """Cloud providers that currently receive mail content, per task (docs/PRIVACY.md)."""
    config = await _config(db, settings.llm)
    if not config.cloud_enabled:
        return AIStatusRead(cloud=[])
    records = {r.name: r for r in await store.list_providers(db)}
    usage: dict[str, list[LLMTask]] = {}
    for task in LLMTask:
        endpoint = config.assignment(task).endpoint
        if endpoint.is_cloud:
            usage.setdefault(endpoint.name, []).append(task)
    return AIStatusRead(
        cloud=[
            CloudUsage(
                provider=name,
                display_name=(
                    records[name].display_name
                    if name in records and name not in config.env_endpoints
                    else name
                ),
                tasks=tasks,
            )
            for name, tasks in usage.items()
        ]
    )
