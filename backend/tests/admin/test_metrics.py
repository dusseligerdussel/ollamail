"""Prometheus endpoint of the api: off by default, token check, process and database
metrics without content."""

from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from prometheus_client import CollectorRegistry, generate_latest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import metrics
from app.ai.llm.metrics import LLMCallMetrics, observe
from app.core.config import DatabaseSettings, MetricsSettings, Settings
from app.core.db import Database
from app.mail.models import Mailbox, MailboxType, Message, SyncState
from app.main import create_app
from app.processing.models import MessageProcessing, StepStatus
from tests.factories import make_user

SUBJECT = "Salary review for alice@example.org"
UNREACHABLE_DB = "postgresql+asyncpg://ollamail:ollamail@127.0.0.1:1/ollamail"


def _with_metrics(settings: Settings, **values: object) -> Settings:
    return settings.model_copy(
        update={"metrics": MetricsSettings.model_validate({"enabled": True, **values})}
    )


async def _get(settings: Settings, headers: dict[str, str] | None = None) -> tuple[int, str]:
    app = create_app(settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/metrics", headers=headers)
    await app.state.database.dispose()
    return response.status_code, response.text


async def test_metrics_are_off_by_default(settings: Settings) -> None:
    status, _ = await _get(settings)

    assert status == 404


async def test_metrics_are_not_in_the_openapi_schema(settings: Settings) -> None:
    app = create_app(_with_metrics(settings))

    assert "/metrics" not in app.openapi()["paths"]
    await app.state.database.dispose()


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        (None, 401),
        ({"Authorization": "Bearer wrong"}, 401),
        ({"Authorization": "Basic s3cret-token"}, 401),
    ],
)
async def test_token_is_required_when_configured(
    settings: Settings, headers: dict[str, str] | None, expected: int
) -> None:
    status, _ = await _get(_with_metrics(settings, token="s3cret-token"), headers)

    assert status == expected


async def test_unreachable_database_still_serves_process_metrics(settings: Settings) -> None:
    settings = _with_metrics(settings, token="s3cret-token").model_copy(
        update={"database": DatabaseSettings.model_validate({"url": UNREACHABLE_DB})}
    )
    observe(
        LLMCallMetrics(
            task="triage",
            operation="structured",
            endpoint="default",
            provider="ollama",
            model="qwen2.5:3b",
            prompt_version="triage-v1",
            duration_ms=2000,
            success=True,
            prompt_tokens=300,
            completion_tokens=40,
        )
    )

    status, body = await _get(settings, {"Authorization": "Bearer s3cret-token"})

    assert status == 200
    assert "ollamail_metrics_database_up 0.0" in body
    assert 'ollamail_llm_request_duration_seconds_count{endpoint="default"' in body
    assert "ollamail_llm_completion_tokens_per_second_bucket" in body


@pytest.mark.db
async def test_endpoint_reads_the_database(settings: Settings) -> None:
    status, body = await _get(_with_metrics(settings))

    assert status == 200
    assert "ollamail_metrics_database_up 1.0" in body
    assert "# TYPE ollamail_queue_jobs gauge" in body


def _render(families: list[metrics.Metric]) -> str:
    registry = CollectorRegistry(auto_describe=False)
    registry.register(metrics._Fixed(families))
    return generate_latest(registry).decode()


@pytest.mark.db
async def test_database_metrics_hold_ids_and_counts_only(db_session: AsyncSession) -> None:
    owner = await make_user(db_session, display_name="Erika Muster")
    mailbox = Mailbox(
        type=MailboxType.IMAP,
        display_name="Erika private",
        address="erika@example.org",
        owner_user_id=owner.id,
    )
    db_session.add(mailbox)
    await db_session.flush()
    message = Message(mailbox_id=mailbox.id, remote_ref="1", subject=SUBJECT)
    db_session.add(message)
    await db_session.flush()
    db_session.add_all(
        [
            MessageProcessing(
                message_id=message.id, step="triage", version=1, status=StepStatus.FAILED
            ),
            MessageProcessing(
                message_id=message.id, step="todos", version=1, status=StepStatus.PENDING
            ),
            MessageProcessing(
                message_id=message.id, step="normalize", version=1, status=StepStatus.DONE
            ),
            SyncState(
                mailbox_id=mailbox.id,
                last_error="auth_failed",
                last_synced_at=datetime(2026, 10, 1, tzinfo=UTC),
            ),
        ]
    )
    await db_session.execute(text("DELETE FROM procrastinate_jobs"))
    await db_session.execute(
        text(
            "INSERT INTO procrastinate_jobs (queue_name, task_name, priority, status, args) "
            "VALUES ('llm', 'processing.run_step', 10, 'todo', '{}'), "
            "('llm', 'processing.run_step', 10, 'todo', '{}'), "
            "('sync', 'mail.sync_mailbox', 0, 'failed', '{}')"
        )
    )
    await db_session.flush()

    body = _render(await metrics.collect_database_metrics(db_session))

    box = str(mailbox.id)
    assert 'ollamail_queue_jobs{priority="10",queue="llm",status="todo"} 2.0' in body
    assert 'ollamail_queue_failed_jobs{queue="sync",task="mail.sync_mailbox"} 1.0' in body
    assert 'ollamail_processing_steps{status="failed",step="triage"} 1.0' in body
    assert 'ollamail_processing_steps{status="pending",step="todos"} 1.0' in body
    assert 'step="normalize"' not in body
    assert f'ollamail_mailbox_processing_steps{{mailbox_id="{box}",status="failed"}} 1.0' in body
    assert f'ollamail_mailbox_sync_phase{{mailbox_id="{box}",phase="error"}} 1.0' in body
    assert f'ollamail_mailbox_sync_error{{code="auth_failed",mailbox_id="{box}"}} 1.0' in body
    assert f'ollamail_mailbox_last_sync_timestamp_seconds{{mailbox_id="{box}"}}' in body
    for content in (SUBJECT, "example.org", "Erika"):
        assert content not in body


async def test_database_metrics_are_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    reads: list[int] = []
    now = [0.0]

    async def read(self: metrics.DatabaseMetrics) -> bytes:
        reads.append(1)
        return f"read {len(reads)}\n".encode()

    monkeypatch.setattr(metrics.DatabaseMetrics, "_read", read)
    database = Database(DatabaseSettings.model_validate({"url": UNREACHABLE_DB}))
    cache = metrics.DatabaseMetrics(database, max_age=60, clock=lambda: now[0])

    assert await cache.render() == b"read 1\n"
    now[0] = 59
    assert await cache.render() == b"read 1\n"
    now[0] = 60
    assert await cache.render() == b"read 2\n"
    await database.dispose()


def test_metrics_settings_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMAIL_METRICS_ENABLED", "true")
    monkeypatch.setenv("OLLAMAIL_METRICS_TOKEN", "")
    monkeypatch.setenv("OLLAMAIL_METRICS_WORKER_PORT", "0")

    settings = MetricsSettings()

    assert settings.enabled is True
    assert settings.token is None
    assert settings.worker_port == 0
    assert MetricsSettings(token=SecretStr("x")).token is not None
