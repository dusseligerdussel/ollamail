"""Integration tests: mailbox API with sign-in and PostgreSQL. Users only reach their own
mailboxes; another user's mailbox behaves like a missing one."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.mail.models import Mailbox
from app.mail.storage import AttachmentStorage
from app.worker import resource_lock
from tests.mail.api.conftest import (
    IMAP_SETTINGS,
    PASSWORD,
    FakeServer,
    add_mailbox,
    mailbox_body,
    run_sync,
)

pytestmark = pytest.mark.db


async def mailbox_count(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(Mailbox)) or 0


# -- authentication and access --------------------------------------------------------


async def test_requires_sign_in(anonymous: AsyncClient) -> None:
    some_id = uuid.uuid4()
    assert (await anonymous.get("/mailboxes")).status_code == 401
    assert (await anonymous.post("/mailboxes", json=mailbox_body())).status_code == 401
    assert (await anonymous.post("/mailboxes/test", json=mailbox_body())).status_code == 401
    assert (
        await anonymous.post("/mailboxes/autodiscover", json={"address": "a@example.org"})
    ).status_code == 401
    assert (await anonymous.delete(f"/mailboxes/{some_id}")).status_code == 401


async def test_users_never_see_other_users_mailboxes(
    erika: AsyncClient, bob: AsyncClient, sync_requests: list[uuid.UUID], db_session: AsyncSession
) -> None:
    mailbox = await add_mailbox(erika)
    mailbox_id = mailbox["id"]
    sync_requests.clear()

    assert (await bob.get("/mailboxes")).json() == []
    for method, path, body in [
        ("GET", f"/mailboxes/{mailbox_id}", None),
        ("GET", f"/mailboxes/{mailbox_id}/status", None),
        ("GET", f"/mailboxes/{mailbox_id}/folders", None),
        ("PATCH", f"/mailboxes/{mailbox_id}", {"display_name": "Mine now"}),
        (
            "PATCH",
            f"/mailboxes/{mailbox_id}/folders",
            {"folders": [{"id": mailbox_id, "sync_enabled": False}]},
        ),
        ("POST", f"/mailboxes/{mailbox_id}/sync", None),
        ("DELETE", f"/mailboxes/{mailbox_id}", None),
    ]:
        response = await bob.request(method, path, json=body)
        assert response.status_code == 404, (method, path)
        # Same answer as for an ID that does not exist.
        missing = await bob.request(
            method, path.replace(str(mailbox_id), str(uuid.uuid4())), json=body
        )
        assert missing.status_code == 404

    assert sync_requests == []
    unchanged = await erika.get(f"/mailboxes/{mailbox_id}")
    assert unchanged.status_code == 200
    assert unchanged.json()["display_name"] == "erika@example.org"
    assert await mailbox_count(db_session) == 1


async def test_list_contains_only_own_mailboxes(erika: AsyncClient, bob: AsyncClient) -> None:
    await add_mailbox(erika, display_name="Work")
    await add_mailbox(erika, address="erika.private@example.net", display_name="Private")
    await add_mailbox(bob, address="bob@example.org")

    names = [m["display_name"] for m in (await erika.get("/mailboxes")).json()]
    assert names == ["Private", "Work"]
    assert [m["address"] for m in (await bob.get("/mailboxes")).json()] == ["bob@example.org"]


# -- connection test and autodiscovery ------------------------------------------------


async def test_connection_test_lists_folders_without_saving(
    erika: AsyncClient, server: FakeServer, db_session: AsyncSession
) -> None:
    response = await erika.post("/mailboxes/test", json=mailbox_body())

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["error"] is None
    assert {(f["remote_id"], f["role"]) for f in body["folders"]} == {
        ("INBOX", "inbox"),
        ("Archive", "archive"),
        ("Trash", "trash"),
    }
    assert server.connections[-1].settings == IMAP_SETTINGS
    assert await mailbox_count(db_session) == 0


async def test_connection_test_reports_error_code(erika: AsyncClient) -> None:
    response = await erika.post(
        "/mailboxes/test", json=mailbox_body(credentials={"password": "wrong"})
    )

    assert response.status_code == 200
    assert response.json() == {"ok": False, "error": "authentication_failed", "folders": []}


async def test_unavailable_type_is_rejected(erika: AsyncClient) -> None:
    for path in ("/mailboxes/test", "/mailboxes"):
        response = await erika.post(path, json=mailbox_body(type="gmail"))
        assert response.status_code == 422
        assert response.json()["error_code"] == "type_unavailable"


async def test_autodiscover_suggests_known_provider_and_guesses(erika: AsyncClient) -> None:
    known = await erika.post("/mailboxes/autodiscover", json={"address": "erika@gmx.de"})
    guessed = await erika.post("/mailboxes/autodiscover", json={"address": "erika@example.org"})

    assert known.status_code == 200
    assert known.json()["suggestions"] == [
        {
            "type": "imap",
            "provider_settings": {"host": "imap.gmx.net", "port": 993, "security": "tls"},
            "source": "known",
            "hints": ["enable_imap"],
        }
    ]
    hosts = [s["provider_settings"]["host"] for s in guessed.json()["suggestions"]]
    assert hosts == ["imap.example.org", "mail.example.org"]
    invalid = await erika.post("/mailboxes/autodiscover", json={"address": "not-an-address"})
    assert invalid.status_code == 422


# -- create ---------------------------------------------------------------------------


async def test_create_tests_connection_stores_encrypted_and_starts_sync(
    erika: AsyncClient, sync_requests: list[uuid.UUID], db_session: AsyncSession
) -> None:
    response = await erika.post(
        "/mailboxes",
        json=mailbox_body(sync_settings={"initial_sync_days": 30, "excluded_folders": ["Archive"]}),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["display_name"] == "erika@example.org"
    assert body["provider_settings"] == IMAP_SETTINGS
    assert body["has_credentials"] is True
    assert "credentials" not in body
    assert PASSWORD not in response.text
    assert body["sync_enabled"] is True
    assert body["sync_settings"]["initial_sync_days"] == 30
    assert body["sync_settings"]["excluded_folders"] == ["Archive"]
    assert body["sync_settings"]["excluded_roles"] == ["trash", "junk"]
    assert body["status"]["phase"] == "pending"
    assert body["status"]["message_count"] == 0
    assert sync_requests == [uuid.UUID(body["id"])]

    raw = await db_session.scalar(
        text("SELECT credentials::text FROM mail_mailboxes WHERE id = :id"), {"id": body["id"]}
    )
    assert raw is not None
    assert PASSWORD not in raw


async def test_create_rejects_failed_connection(
    erika: AsyncClient, sync_requests: list[uuid.UUID], db_session: AsyncSession
) -> None:
    response = await erika.post("/mailboxes", json=mailbox_body(credentials={"password": "wrong"}))

    assert response.status_code == 422
    assert response.json()["error_code"] == "authentication_failed"
    assert await mailbox_count(db_session) == 0
    assert sync_requests == []


async def test_create_rejects_duplicate(erika: AsyncClient, bob: AsyncClient) -> None:
    await add_mailbox(erika)

    duplicate = await erika.post("/mailboxes", json=mailbox_body(address="Erika@Example.org"))

    assert duplicate.status_code == 409
    # The same address for another user is a separate mailbox.
    assert (await bob.post("/mailboxes", json=mailbox_body())).status_code == 201


async def test_create_paused_does_not_sync(
    erika: AsyncClient, sync_requests: list[uuid.UUID]
) -> None:
    body = await add_mailbox(erika, sync_enabled=False, display_name="Later")

    assert body["sync_enabled"] is False
    assert body["status"]["phase"] == "paused"  # type: ignore[index]
    assert sync_requests == []


async def test_create_rejects_oversized_input(erika: AsyncClient) -> None:
    too_large = {"host": "x" * 20_000}
    response = await erika.post("/mailboxes", json=mailbox_body(provider_settings=too_large))
    assert response.status_code == 422


# -- update, pause, resume ------------------------------------------------------------


async def test_pause_and_resume(erika: AsyncClient, sync_requests: list[uuid.UUID]) -> None:
    mailbox_id = (await add_mailbox(erika))["id"]
    sync_requests.clear()

    paused = await erika.patch(f"/mailboxes/{mailbox_id}", json={"sync_enabled": False})
    assert paused.status_code == 200
    assert paused.json()["status"]["phase"] == "paused"
    assert (await erika.post(f"/mailboxes/{mailbox_id}/sync")).status_code == 409
    assert sync_requests == []

    resumed = await erika.patch(f"/mailboxes/{mailbox_id}", json={"sync_enabled": True})
    assert resumed.json()["sync_enabled"] is True
    assert sync_requests == [uuid.UUID(str(mailbox_id))]


async def test_rename_does_not_reconnect(erika: AsyncClient, server: FakeServer) -> None:
    mailbox_id = (await add_mailbox(erika))["id"]
    connections = len(server.connections)

    response = await erika.patch(f"/mailboxes/{mailbox_id}", json={"display_name": " Work "})

    assert response.json()["display_name"] == "Work"
    assert len(server.connections) == connections


async def test_changed_credentials_are_tested_before_saving(
    erika: AsyncClient, server: FakeServer, db_session: AsyncSession
) -> None:
    mailbox_id = (await add_mailbox(erika))["id"]

    rejected = await erika.patch(
        f"/mailboxes/{mailbox_id}",
        json={"credentials": {"password": "wrong"}, "display_name": "Changed"},
    )
    assert rejected.status_code == 422
    assert rejected.json()["error_code"] == "authentication_failed"
    mailbox = await db_session.get(Mailbox, uuid.UUID(str(mailbox_id)), populate_existing=True)
    assert mailbox is not None
    assert mailbox.credentials == {"password": PASSWORD}
    assert mailbox.display_name == "erika@example.org"

    # New settings are tested with the stored credentials.
    accepted = await erika.patch(
        f"/mailboxes/{mailbox_id}",
        json={"provider_settings": {**IMAP_SETTINGS, "host": "mail.example.org"}},
    )
    assert accepted.status_code == 200
    assert server.connections[-1].credentials == {"password": PASSWORD}
    assert accepted.json()["provider_settings"]["host"] == "mail.example.org"


async def test_sync_settings_are_merged(erika: AsyncClient) -> None:
    mailbox_id = (await add_mailbox(erika, sync_settings={"initial_sync_days": 30}))["id"]

    changed = await erika.patch(
        f"/mailboxes/{mailbox_id}", json={"sync_settings": {"excluded_roles": ["junk"]}}
    )
    assert changed.json()["sync_settings"]["initial_sync_days"] == 30
    assert changed.json()["sync_settings"]["excluded_roles"] == ["junk"]

    reset = await erika.patch(
        f"/mailboxes/{mailbox_id}", json={"sync_settings": {"initial_sync_days": None}}
    )
    assert reset.json()["sync_settings"]["initial_sync_days"] is None
    assert reset.json()["sync_settings"]["excluded_roles"] == ["junk"]


# -- sync, status, folders ------------------------------------------------------------


async def test_manual_sync_is_queued(erika: AsyncClient, sync_requests: list[uuid.UUID]) -> None:
    mailbox_id = (await add_mailbox(erika))["id"]
    sync_requests.clear()

    response = await erika.post(f"/mailboxes/{mailbox_id}/sync")

    assert response.status_code == 202
    assert response.json() == {"queued": True}
    assert sync_requests == [uuid.UUID(str(mailbox_id))]


async def test_status_after_sync(
    erika: AsyncClient,
    server: FakeServer,
    storage: AttachmentStorage,
    db_session: AsyncSession,
) -> None:
    mailbox_id = (await add_mailbox(erika))["id"]
    await run_sync(db_session, str(mailbox_id), server, storage)

    status = (await erika.get(f"/mailboxes/{mailbox_id}/status")).json()

    assert status["phase"] == "idle"
    assert status["last_error"] is None
    assert status["last_synced_at"] is not None
    # Trash is excluded by its role.
    assert (status["folders_total"], status["folders_imported"]) == (2, 2)
    assert status["message_count"] == 3
    assert (await erika.get(f"/mailboxes/{mailbox_id}")).json()["status"] == status


async def test_status_shows_mailbox_error(
    erika: AsyncClient,
    server: FakeServer,
    storage: AttachmentStorage,
    db_session: AsyncSession,
) -> None:
    mailbox_id = (await add_mailbox(erika))["id"]
    mailbox = await db_session.get(Mailbox, uuid.UUID(str(mailbox_id)))
    assert mailbox is not None
    mailbox.credentials = {"password": "changed-on-the-server"}
    await db_session.commit()
    await run_sync(db_session, str(mailbox_id), server, storage)

    status = (await erika.get(f"/mailboxes/{mailbox_id}/status")).json()

    assert status["phase"] == "error"
    assert status["last_error"] == "authentication_failed"


async def test_status_shows_queued_sync_job(erika: AsyncClient, db_session: AsyncSession) -> None:
    mailbox_id = (await add_mailbox(erika))["id"]
    await db_session.execute(
        text(
            "INSERT INTO procrastinate_jobs (queue_name, task_name, lock, queueing_lock)"
            " VALUES ('sync', 'mail.sync_mailbox', :lock, :lock)"
        ),
        {"lock": resource_lock("mailbox", str(mailbox_id))},
    )

    status = (await erika.get(f"/mailboxes/{mailbox_id}/status")).json()

    assert (status["phase"], status["sync_queued"]) == ("syncing", True)


async def test_folder_selection(
    erika: AsyncClient,
    server: FakeServer,
    storage: AttachmentStorage,
    db_session: AsyncSession,
    sync_requests: list[uuid.UUID],
) -> None:
    mailbox_id = (await add_mailbox(erika))["id"]
    await run_sync(db_session, str(mailbox_id), server, storage)
    folders = {
        f["remote_id"]: f for f in (await erika.get(f"/mailboxes/{mailbox_id}/folders")).json()
    }

    assert folders["INBOX"]["synced"] is True
    assert folders["INBOX"]["message_count"] == 2
    assert folders["INBOX"]["import_pending"] is False
    assert folders["Trash"]["excluded_by_role"] is True
    assert folders["Trash"]["synced"] is False
    assert folders["Trash"]["message_count"] == 0

    sync_requests.clear()
    response = await erika.patch(
        f"/mailboxes/{mailbox_id}/folders",
        json={"folders": [{"id": folders["Archive"]["id"], "sync_enabled": False}]},
    )
    assert response.status_code == 200
    archive = next(f for f in response.json() if f["remote_id"] == "Archive")
    assert (archive["sync_enabled"], archive["synced"]) == (False, False)
    mailbox = (await erika.get(f"/mailboxes/{mailbox_id}")).json()
    assert mailbox["sync_settings"]["excluded_folders"] == ["Archive"]
    assert mailbox["status"]["folders_total"] == 1
    assert sync_requests == []

    enabled = await erika.patch(
        f"/mailboxes/{mailbox_id}/folders",
        json={"folders": [{"id": folders["Archive"]["id"], "sync_enabled": True}]},
    )
    assert next(f for f in enabled.json() if f["remote_id"] == "Archive")["synced"] is True
    assert sync_requests == [uuid.UUID(str(mailbox_id))]

    unknown = await erika.patch(
        f"/mailboxes/{mailbox_id}/folders",
        json={"folders": [{"id": str(uuid.uuid4()), "sync_enabled": False}]},
    )
    assert unknown.status_code == 422
    assert unknown.json()["error_code"] == "unknown_folder"
