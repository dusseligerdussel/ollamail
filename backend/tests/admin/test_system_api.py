"""Admin system status: model state and downloads, checklist facts, processing counts.

All data is synthetic."""

import uuid
from typing import Any

import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import system
from app.ai.settings.models import AIModelPull
from app.core.config import AuthSettings, LLMSettings, Settings
from app.core.ids import uuid7
from app.core.jobs import JobQueue
from app.digest.models import DigestUserSettings
from app.mail.models import Mailbox, MailboxType, Message
from app.processing.models import MessageProcessing, StepStatus
from app.users.models import User, UserRole
from tests.auth.conftest import login, make_local_user
from tests.factories import make_user

pytestmark = pytest.mark.db

OLLAMA = "http://ollama.test:11434"
SUBJECT = "Quarterly numbers for alice@example.org"


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "llm": LLMSettings(base_url=OLLAMA, default_chat_model="qwen2.5:3b"),
            "auth": AuthSettings(public_url="https://mail.example.org"),
        }
    )


@pytest.fixture
def queued(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """Records jobs instead of queueing them."""
    calls: dict[str, list[Any]] = {"pulls": [], "messages": []}

    async def ensure_open(self: JobQueue) -> None:
        return None

    async def queue_pull(pull_id: uuid.UUID) -> None:
        calls["pulls"].append(pull_id)

    async def requeue(message_ids: list[uuid.UUID], priority: int) -> None:
        calls["messages"].append((list(message_ids), priority))

    monkeypatch.setattr(JobQueue, "ensure_open", ensure_open)
    monkeypatch.setattr(system.pulls, "queue_pull", queue_pull)
    monkeypatch.setattr(system, "requeue_messages", requeue)
    return calls


async def _sign_in(client: AsyncClient, db: AsyncSession, role: UserRole) -> User:
    user = await make_local_user(db, f"{role}@example.org", role=role)
    assert (await login(client, user.email)).status_code == 200
    return user


async def _mailbox(db: AsyncSession, owner: User | None, name: str | None = None) -> Mailbox:
    address = f"{uuid.uuid4().hex[:8]}@example.org"
    mailbox = Mailbox(
        type=MailboxType.IMAP,
        display_name=name or address,
        address=address,
        owner_user_id=owner.id if owner else None,
        is_shared=owner is None,
    )
    db.add(mailbox)
    await db.flush()
    return mailbox


async def _message(db: AsyncSession, mailbox: Mailbox, **steps: StepStatus) -> uuid.UUID:
    message_id = uuid7()
    db.add(
        Message(id=message_id, mailbox_id=mailbox.id, remote_ref=str(message_id), subject=SUBJECT)
    )
    await db.flush()
    for step, status in steps.items():
        db.add(
            MessageProcessing(
                message_id=message_id,
                step=step,
                version=1,
                status=status,
                attempts=3,
                error_code="llm_unavailable_error" if status == StepStatus.FAILED else None,
            )
        )
    await db.flush()
    return message_id


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/admin/system/models"),
        ("POST", "/admin/system/models/pull"),
        ("GET", "/admin/system/overview"),
        ("POST", f"/admin/system/mailboxes/{uuid.uuid4()}/retry-failed"),
        ("POST", f"/admin/system/mailboxes/{uuid.uuid4()}/include-older"),
    ],
)
async def test_only_admins_have_access(
    db_client: AsyncClient, db_session: AsyncSession, method: str, path: str
) -> None:
    body = {"endpoint": "default", "model": "qwen2.5:3b"}
    assert (await db_client.request(method, path, json=body)).status_code == 401
    await _sign_in(db_client, db_session, UserRole.USER)
    assert (await db_client.request(method, path, json=body)).status_code == 403


@respx.mock
async def test_models_report_installed_and_missing(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _sign_in(db_client, db_session, UserRole.ADMIN)
    respx.get(f"{OLLAMA}/api/tags").respond(200, json={"models": [{"name": "qwen2.5:3b"}]})

    response = await db_client.get("/admin/system/models")

    assert response.status_code == 200
    by_task = {item["task"]: item for item in response.json()}
    assert by_task["triage"]["state"] == "installed"
    assert by_task["triage"]["can_pull"] is False
    assert by_task["embeddings"] == {
        "task": "embeddings",
        "endpoint": "default",
        "provider": "ollama",
        "model": "bge-m3",
        "state": "missing",
        "can_pull": True,
        "pull": None,
    }


@respx.mock
async def test_models_report_unreachable_endpoint(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _sign_in(db_client, db_session, UserRole.ADMIN)
    respx.get(f"{OLLAMA}/api/tags").respond(503)

    response = await db_client.get("/admin/system/models")

    assert {item["state"] for item in response.json()} == {"unreachable"}
    assert not any(item["can_pull"] for item in response.json())


@respx.mock
async def test_pull_queues_a_job_once(
    db_client: AsyncClient, db_session: AsyncSession, queued: dict[str, list[Any]]
) -> None:
    await _sign_in(db_client, db_session, UserRole.ADMIN)
    respx.get(f"{OLLAMA}/api/tags").respond(200, json={"models": []})
    body = {"endpoint": "default", "model": "bge-m3"}

    first = await db_client.post("/admin/system/models/pull", json=body)
    second = await db_client.post("/admin/system/models/pull", json=body)
    listing = await db_client.get("/admin/system/models")

    assert first.status_code == second.status_code == 202
    assert first.json()["status"] == "queued"
    (pull,) = (await db_session.scalars(select(AIModelPull))).all()
    assert (pull.endpoint, pull.model) == ("default", "bge-m3:latest")
    # The second request finds the queued pull and does not queue another job.
    assert queued["pulls"] == [pull.id]
    by_task = {item["task"]: item for item in listing.json()}
    assert by_task["embeddings"]["pull"]["status"] == "queued"


async def test_pull_rejects_unassigned_models(
    db_client: AsyncClient, db_session: AsyncSession, queued: dict[str, list[Any]]
) -> None:
    await _sign_in(db_client, db_session, UserRole.ADMIN)

    response = await db_client.post(
        "/admin/system/models/pull", json={"endpoint": "default", "model": "anything:70b"}
    )

    assert response.status_code == 422
    assert queued["pulls"] == []


async def test_overview_shows_counts_and_never_content(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin = await _sign_in(db_client, db_session, UserRole.ADMIN)
    owner = await make_user(db_session, display_name="Erika Muster")
    busy = await _mailbox(db_session, owner)
    quiet = await _mailbox(db_session, None, "Support")
    await _message(db_session, busy, normalize=StepStatus.DONE, triage=StepStatus.FAILED)
    await _message(db_session, busy, normalize=StepStatus.PENDING, triage=StepStatus.PENDING)
    await _message(db_session, quiet, normalize=StepStatus.DONE, triage=StepStatus.SKIPPED)
    db_session.add(DigestUserSettings(user_id=admin.id, enabled=True))
    await db_session.flush()

    response = await db_client.get("/admin/system/overview")

    assert response.status_code == 200
    data = response.json()
    assert SUBJECT not in response.text
    assert "@example.org" not in response.text
    assert data["mailbox_count"] == 2
    assert data["digest_enabled"] is True
    assert data["digest_scheduler_enabled"] is True
    assert data["public_url_set"] is True
    first, second = data["mailboxes"]
    assert first["id"] == str(busy.id)
    # Names of personal mailboxes default to their address: only type and owner are shown.
    assert (first["display_name"], first["type"]) == (None, "imap")
    assert second["display_name"] == "Support"
    assert (first["pending"], first["running"], first["failed"]) == (2, 0, 1)
    assert first["owner_name"] == "Erika Muster"
    assert first["sync_phase"] == "pending"
    assert second["id"] == str(quiet.id)
    assert second["owner_name"] is None
    assert (second["pending"], second["failed"]) == (0, 0)
    assert (first["skipped_messages"], second["skipped_messages"]) == (0, 1)
    assert first["include_older"] is second["include_older"] is False


async def test_retry_failed_resets_only_failed_steps(
    db_client: AsyncClient, db_session: AsyncSession, queued: dict[str, list[Any]]
) -> None:
    await _sign_in(db_client, db_session, UserRole.ADMIN)
    owner = await make_user(db_session)
    mailbox = await _mailbox(db_session, owner)
    other = await _mailbox(db_session, owner, "Other")
    failed = await _message(
        db_session, mailbox, normalize=StepStatus.DONE, triage=StepStatus.FAILED
    )
    await _message(db_session, mailbox, normalize=StepStatus.DONE, triage=StepStatus.DONE)
    elsewhere = await _message(db_session, other, triage=StepStatus.FAILED)

    response = await db_client.post(f"/admin/system/mailboxes/{mailbox.id}/retry-failed")

    assert response.status_code == 200
    assert response.json() == {"queued": 1}
    assert queued["messages"] == [([failed], -10)]
    rows = {
        (row.message_id, row.step): row
        for row in await db_session.scalars(select(MessageProcessing))
    }
    assert rows[(failed, "triage")].status == StepStatus.PENDING
    assert rows[(failed, "triage")].error_code is None
    assert rows[(failed, "triage")].attempts == 0
    assert rows[(failed, "normalize")].status == StepStatus.DONE
    assert rows[(elsewhere, "triage")].status == StepStatus.FAILED
    missing = await db_client.post(f"/admin/system/mailboxes/{uuid.uuid4()}/retry-failed")
    assert missing.status_code == 404


async def test_include_older_queues_skipped_mail(
    db_client: AsyncClient, db_session: AsyncSession, queued: dict[str, list[Any]]
) -> None:
    await _sign_in(db_client, db_session, UserRole.ADMIN)
    owner = await make_user(db_session)
    mailbox = await _mailbox(db_session, owner)
    other = await _mailbox(db_session, owner, "Other")
    old = await _message(db_session, mailbox, index=StepStatus.DONE, triage=StepStatus.SKIPPED)
    await _message(db_session, mailbox, index=StepStatus.DONE, triage=StepStatus.DONE)
    elsewhere = await _message(db_session, other, triage=StepStatus.SKIPPED)

    response = await db_client.post(f"/admin/system/mailboxes/{mailbox.id}/include-older")

    assert response.status_code == 200
    assert response.json() == {"queued": 1}
    # Behind new mail.
    assert queued["messages"] == [([old], -10)]
    rows = {
        (row.message_id, row.step): row.status
        for row in await db_session.scalars(select(MessageProcessing))
    }
    assert rows[(old, "triage")] == StepStatus.PENDING
    assert rows[(old, "index")] == StepStatus.DONE
    assert rows[(elsewhere, "triage")] == StepStatus.SKIPPED
    overview = (await db_client.get("/admin/system/overview")).json()
    flags = {item["id"]: item["include_older"] for item in overview["mailboxes"]}
    assert flags == {str(mailbox.id): True, str(other.id): False}

    missing = await db_client.post(f"/admin/system/mailboxes/{uuid.uuid4()}/include-older")
    assert missing.status_code == 404
