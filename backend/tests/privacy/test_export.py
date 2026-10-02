"""Personal data export (Art. 15/20): own data only, background job, expiring download."""

import io
import json
import uuid
import zipfile
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditAction
from app.digest.storage import DigestStorage
from app.mail.storage import AttachmentStorage
from app.privacy import export, exports
from app.privacy.models import DataExport, ExportStatus
from app.privacy.router import get_export_enqueuer
from app.privacy.storage import ExportStorage
from tests.audit.conftest import audit_rows
from tests.privacy.conftest import FakeServer, current_user_id, seed_user_data, utcnow

pytestmark = pytest.mark.db


async def run_job(session: AsyncSession, data_dir: Path, export_id: str) -> None:
    """What the worker's ``privacy.export`` job does."""
    await exports.run_export(
        session,
        uuid.UUID(export_id),
        storage=ExportStorage(data_dir),
        digest_storage=DigestStorage(data_dir),
        expiry_hours=24,
    )


async def test_export_contains_own_data_and_nothing_of_other_users(
    app: FastAPI,
    erika: AsyncClient,
    bob: AsyncClient,
    server: FakeServer,
    storage: AttachmentStorage,
    data_dir: Path,
    db_session: AsyncSession,
) -> None:
    own = await seed_user_data(
        db_session, erika, server, storage, data_dir, address="erika@example.org", marker="erika"
    )
    other = await seed_user_data(
        db_session, bob, server, storage, data_dir, address="bob@example.org", marker="bob"
    )
    queued: list[uuid.UUID] = []

    async def record(export_id: uuid.UUID) -> None:
        queued.append(export_id)

    app.dependency_overrides[get_export_enqueuer] = lambda: record

    response = await erika.post("/privacy/exports")

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "pending"
    assert queued == [uuid.UUID(body["id"])]
    # A second request while it waits returns the same export.
    assert (await erika.post("/privacy/exports")).json()["id"] == body["id"]
    assert len(queued) == 1
    # Not downloadable before it is ready.
    download = f"/privacy/exports/{body['id']}/download"
    assert (await erika.get(download)).status_code == 404

    await run_job(db_session, data_dir, body["id"])

    [listed] = (await erika.get("/privacy/exports")).json()
    assert listed["status"] == "ready" and listed["size"] > 0
    assert listed["expires_at"] is not None
    response = await erika.get(download)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["cache-control"] == "no-store"
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    names = set(archive.namelist())
    assert {
        "manifest.json",
        "profile.json",
        "mailboxes.json",
        "triage.json",
        "todos.json",
        "digests.json",
        "conversations.json",
        f"digests/{own.digest_id}.mp3",
    } <= names
    profile = json.loads(archive.read("profile.json"))
    assert profile["user"]["email"] == "erika@example.org"
    assert [i["provider"] for i in profile["identities"]] == ["local"]
    factors = profile["second_factors"]
    assert [p["name"] for p in factors["passkeys"]] == ["Passkey erika"]
    assert factors["recovery_codes_remaining"] == 1
    # Names and dates only: no TOTP secret, keys or code hashes.
    assert "JBSWY3DPEHPK3PXP" not in archive.read("profile.json").decode()
    assert "public_key" not in json.dumps(factors)
    assert [t["title"] for t in json.loads(archive.read("todos.json"))] == ["Todo erika"]
    triage = json.loads(archive.read("triage.json"))
    assert [c["name"] for c in triage["categories"] if c["own"]] == ["Category erika"]
    assert [r["sender"] for r in triage["sender_rules"]] == ["sender-erika.example.org"]
    assert len(triage["corrections"]) == 1
    assert {r["reason"] for r in triage["results"]} == {"Reason erika"}
    [conversation] = json.loads(archive.read("conversations.json"))
    assert [m["content"] for m in conversation["messages"]] == ["Ask erika", "Answer erika [1]"]
    assert conversation["messages"][1]["citations"][0]["snippet"] == "Snippet erika"
    digests = json.loads(archive.read("digests.json"))
    assert [d["script"] for d in digests["digests"]] == ["Script erika"]
    assert "feed_token_hash" not in json.dumps(digests)
    assert archive.read(f"digests/{own.digest_id}.mp3") == b"ID3 audio erika"
    mailboxes = json.loads(archive.read("mailboxes.json"))
    assert [m["address"] for m in mailboxes] == ["erika@example.org"]
    assert "credentials" not in json.dumps(mailboxes)

    # Nothing of the other user: no IDs, no texts, no files.
    everything = b"".join(archive.read(name) for name in names).decode(errors="replace")
    for value in (
        "bob",
        str(other.user_id),
        str(other.mailbox_id),
        str(other.todo_id),
        str(other.category_id),
        str(other.conversation_id),
        str(other.digest_id),
    ):
        assert value not in everything.lower(), value

    # Only the owner can download or delete it.
    assert (await bob.get(download)).status_code == 404
    assert (await bob.delete(f"/privacy/exports/{body['id']}")).status_code == 404
    assert (await bob.get("/privacy/exports")).json() == []

    stages = [
        row.details["stage"] for row in await audit_rows(db_session, AuditAction.DATA_EXPORTED)
    ]
    assert stages == ["requested", "downloaded"]

    assert (await erika.delete(f"/privacy/exports/{body['id']}")).status_code == 204
    assert not ExportStorage(data_dir).path(own.user_id, uuid.UUID(body["id"])).exists()
    assert (await erika.get(download)).status_code == 404


async def test_expired_exports_cannot_be_downloaded_and_are_cleaned_up(
    erika: AsyncClient, data_dir: Path, db_session: AsyncSession
) -> None:
    user_id = await current_user_id(erika)
    item, _ = await exports.request_export(db_session, user_id, expiry_hours=24)
    await db_session.commit()
    await run_job(db_session, data_dir, str(item.id))
    store = ExportStorage(data_dir)
    path = store.path(user_id, item.id)
    assert path.is_file()
    # A file that belongs to no export (e.g. an interrupted run) is removed as well.
    orphan = store.path(user_id, uuid.uuid4())
    orphan.write_bytes(b"leftover")

    item = await db_session.get(DataExport, item.id, populate_existing=True)
    assert item is not None and item.status is ExportStatus.READY
    item.expires_at = utcnow() - timedelta(minutes=1)
    await db_session.commit()

    assert (await erika.get(f"/privacy/exports/{item.id}/download")).status_code == 404
    assert (await erika.get("/privacy/exports")).json() == []

    result = await exports.cleanup_exports(db_session, store, now=utcnow())

    assert (result.expired, result.files) == (1, 1)
    assert not path.exists() and not orphan.exists()
    assert await db_session.get(DataExport, item.id, populate_existing=True) is None


async def test_failed_export_leaves_no_file(
    erika: AsyncClient,
    data_dir: Path,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = await current_user_id(erika)
    item, _ = await exports.request_export(db_session, user_id, expiry_hours=24)
    await db_session.commit()

    def broken(*args: object, **kwargs: object) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(export, "write_zip", broken)
    with pytest.raises(OSError):
        await run_job(db_session, data_dir, str(item.id))

    failed = await db_session.get(DataExport, item.id, populate_existing=True)
    assert failed is not None
    assert (failed.status, failed.error_code) == (ExportStatus.FAILED, "os_error")
    assert not any((ExportStorage(data_dir).base / str(user_id)).glob("*"))
