"""Integration tests: mail actions (#148), i.e. archive, move, trash, undo and flags, with
sign-in, PostgreSQL and the fake mail server, including the ``act`` permission in shared
mailboxes and the audit log. All names, addresses and contents are invented."""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.audit.events import AuditAction
from app.mail import access
from app.mail.access import MailboxPermission
from app.mail.api.messages import get_flag_writer
from app.mail.models import (
    AssignmentPermission,
    Folder,
    FolderRole,
    Mailbox,
    MailboxAssignment,
    MailboxType,
    Message,
)
from app.mail.storage import AttachmentStorage
from app.users.models import User
from tests.audit.conftest import audit_rows
from tests.mail.api.conftest import IMAP_SETTINGS, PASSWORD, FakeServer, add_mailbox, run_sync

pytestmark = pytest.mark.db


@pytest.fixture
def flag_writes() -> list[uuid.UUID]:
    return []


@pytest.fixture(autouse=True)
def _record_flag_writes(app: FastAPI, flag_writes: list[uuid.UUID]) -> Iterator[None]:
    async def record(message_id: uuid.UUID) -> None:
        flag_writes.append(message_id)

    app.dependency_overrides[get_flag_writer] = lambda: record
    yield


async def synced(
    client: AsyncClient, db_session: AsyncSession, server: FakeServer, storage: AttachmentStorage
) -> uuid.UUID:
    mailbox = await add_mailbox(client)
    await run_sync(db_session, str(mailbox["id"]), server, storage)
    return uuid.UUID(str(mailbox["id"]))


async def inbox_ids(client: AsyncClient, **params: object) -> list[str]:
    response = await client.get("/messages", params=params)
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["items"]]


async def folder(db_session: AsyncSession, mailbox_id: uuid.UUID, role: FolderRole) -> Folder:
    found = await db_session.scalar(
        select(Folder).where(Folder.mailbox_id == mailbox_id, Folder.role == role)
    )
    assert found is not None
    return found


async def stored(db_session: AsyncSession, message_id: str) -> Message:
    message = await db_session.scalar(
        select(Message)
        .where(Message.id == uuid.UUID(message_id))
        .options(selectinload(Message.folders))
        .execution_options(populate_existing=True)
    )
    assert message is not None
    return message


async def act(client: AsyncClient, message_id: str, action: str, **body: object) -> dict[str, Any]:
    response = await client.post(f"/messages/{message_id}/actions", json={"action": action, **body})
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


# -- archive, move, trash, undo -----------------------------------------------------------


async def test_requires_sign_in(anonymous: AsyncClient) -> None:
    response = await anonymous.post(f"/messages/{uuid.uuid4()}/actions", json={"action": "trash"})
    assert response.status_code == 401


async def test_archive_moves_the_mail_on_the_server_and_undo_brings_it_back(
    erika: AsyncClient,
    db_session: AsyncSession,
    server: FakeServer,
    storage: AttachmentStorage,
) -> None:
    mailbox_id = await synced(erika, db_session, server, storage)
    inbox, archive = (
        await folder(db_session, mailbox_id, FolderRole.INBOX),
        await folder(db_session, mailbox_id, FolderRole.ARCHIVE),
    )
    message_id, other_id = await inbox_ids(erika)
    old_ref = (await stored(db_session, message_id)).remote_ref

    result = await act(erika, message_id, "archive")

    assert result["folder_ids"] == [str(archive.id)]
    assert result["undo_folder_id"] == str(inbox.id)
    assert result["message"]["id"] == message_id
    move = server.provider.actions[-1]
    assert (move.name, move.args) == ("move", (old_ref, "Archive"))
    message = await stored(db_session, message_id)
    assert message.remote_ref != old_ref
    assert [f.id for f in message.folders] == [archive.id]
    assert await inbox_ids(erika) == [other_id]
    assert message_id in await inbox_ids(erika, mailbox_id=mailbox_id, folder_id=archive.id)
    [entry] = await audit_rows(db_session, AuditAction.MAIL_MOVED)
    assert entry.target_id == str(mailbox_id)
    assert entry.details == {
        "message_id": message_id,
        "action": "archive",
        "folder_id": str(archive.id),
    }

    # The next sync finds the mail in the archive under its new reference: no copy.
    await run_sync(db_session, mailbox_id, server, storage)
    assert await inbox_ids(erika) == [other_id]
    rows = list(await db_session.scalars(select(Message).where(Message.mailbox_id == mailbox_id)))
    assert len(rows) == 3
    assert (await stored(db_session, message_id)).remote_ref == message.remote_ref

    # Undo: back into the inbox.
    result = await act(erika, message_id, "move", folder_id=result["undo_folder_id"])
    assert result["folder_ids"] == [str(inbox.id)]
    assert set(await inbox_ids(erika)) == {message_id, other_id}
    assert len(await audit_rows(db_session, AuditAction.MAIL_MOVED)) == 2


async def test_trash_and_move(
    erika: AsyncClient,
    db_session: AsyncSession,
    server: FakeServer,
    storage: AttachmentStorage,
) -> None:
    mailbox_id = await synced(erika, db_session, server, storage)
    trash = await folder(db_session, mailbox_id, FolderRole.TRASH)
    archive = await folder(db_session, mailbox_id, FolderRole.ARCHIVE)
    first, second = await inbox_ids(erika)

    result = await act(erika, first, "trash")
    assert result["folder_ids"] == [str(trash.id)]
    assert server.provider.actions[-1].args[1] == "Trash"

    result = await act(erika, second, "move", folder_id=str(archive.id))
    assert result["folder_ids"] == [str(archive.id)]
    assert await inbox_ids(erika) == []
    actions = [
        row.details["action"] for row in await audit_rows(db_session, AuditAction.MAIL_MOVED)
    ]
    assert actions == ["trash", "move"]

    # Already there: nothing to do on the server.
    count = len(server.provider.actions)
    result = await act(erika, second, "archive")
    assert result["folder_ids"] == [str(archive.id)]
    assert len(server.provider.actions) == count


async def test_invalid_targets(
    erika: AsyncClient,
    bob: AsyncClient,
    db_session: AsyncSession,
    server: FakeServer,
    storage: AttachmentStorage,
) -> None:
    mailbox_id = await synced(erika, db_session, server, storage)
    message_id = (await inbox_ids(erika))[0]
    path = f"/messages/{message_id}/actions"

    response = await erika.post(path, json={"action": "move"})
    assert (response.status_code, response.json()["error_code"]) == (422, "unknown_folder")
    response = await erika.post(path, json={"action": "move", "folder_id": str(uuid.uuid4())})
    assert (response.status_code, response.json()["error_code"]) == (422, "unknown_folder")
    response = await erika.post(path, json={"action": "delete"})
    assert response.status_code == 422

    # Without an archive folder there is nothing to archive into.
    archive = await folder(db_session, mailbox_id, FolderRole.ARCHIVE)
    archive.role = None
    await db_session.flush()
    response = await erika.post(path, json={"action": "archive"})
    assert (response.status_code, response.json()["error_code"]) == (409, "no_archive_folder")

    # Mails of others are not found.
    response = await bob.post(path, json={"action": "trash"})
    assert response.status_code == 404
    assert [a.name for a in server.provider.actions].count("move") == 0
    assert await audit_rows(db_session, AuditAction.MAIL_MOVED) == []


async def test_server_errors_change_nothing(
    erika: AsyncClient,
    db_session: AsyncSession,
    server: FakeServer,
    storage: AttachmentStorage,
) -> None:
    mailbox_id = await synced(erika, db_session, server, storage)
    inbox = await folder(db_session, mailbox_id, FolderRole.INBOX)
    message_id = (await inbox_ids(erika))[0]
    message = await stored(db_session, message_id)
    ref = message.remote_ref
    # Deleted on the server in the meantime.
    server.provider.delete_message(ref)

    response = await erika.post(f"/messages/{message_id}/actions", json={"action": "archive"})

    assert (response.status_code, response.json()["error_code"]) == (409, "message_not_found")
    message = await stored(db_session, message_id)
    assert (message.remote_ref, [f.id for f in message.folders]) == (ref, [inbox.id])
    assert await audit_rows(db_session, AuditAction.MAIL_MOVED) == []


# -- flags --------------------------------------------------------------------------------


async def test_flag_and_unflag(
    erika: AsyncClient,
    db_session: AsyncSession,
    server: FakeServer,
    storage: AttachmentStorage,
    flag_writes: list[uuid.UUID],
) -> None:
    await synced(erika, db_session, server, storage)
    message_id = (await inbox_ids(erika))[0]
    unread = (await stored(db_session, message_id)).flags

    response = await erika.patch(f"/messages/{message_id}", json={"flagged": True})
    assert response.status_code == 200
    assert response.json()["flagged"] is True
    assert flag_writes == [uuid.UUID(message_id)]
    # Read state untouched.
    assert set((await stored(db_session, message_id)).flags) == {*unread, "flagged"}
    # No change, no write.
    await erika.patch(f"/messages/{message_id}", json={"flagged": True})
    assert len(flag_writes) == 1

    response = await erika.patch(f"/messages/{message_id}", json={"flagged": False, "seen": True})
    assert (response.json()["flagged"], response.json()["unread"]) == (False, False)
    assert len(flag_writes) == 2
    rows = await audit_rows(db_session, AuditAction.MAIL_FLAGGED)
    assert [row.details for row in rows] == [
        {"message_id": message_id, "flagged": True},
        {"message_id": message_id, "flagged": False},
    ]
    # Read/unread alone is not audited.
    await erika.patch(f"/messages/{message_id}", json={"seen": False})
    assert len(await audit_rows(db_session, AuditAction.MAIL_FLAGGED)) == 2


# -- shared mailboxes: act ----------------------------------------------------------------


async def shared_mailbox(
    db_session: AsyncSession, server: FakeServer, storage: AttachmentStorage
) -> uuid.UUID:
    mailbox = Mailbox(
        type=MailboxType.IMAP,
        display_name="Support",
        address="support@example.org",
        owner_user_id=None,
        is_shared=True,
        provider_settings=IMAP_SETTINGS,
        credentials={"password": PASSWORD},
    )
    db_session.add(mailbox)
    await db_session.flush()
    await run_sync(db_session, mailbox.id, server, storage)
    return mailbox.id


async def test_shared_mailboxes_need_an_act_assignment(
    bob: AsyncClient,
    db_session: AsyncSession,
    server: FakeServer,
    storage: AttachmentStorage,
    flag_writes: list[uuid.UUID],
) -> None:
    mailbox_id = await shared_mailbox(db_session, server, storage)
    bob_user = await db_session.scalar(select(User).where(User.email == "bob@example.org"))
    assert bob_user is not None
    assignment = MailboxAssignment(mailbox_id=mailbox_id, user_id=bob_user.id)
    db_session.add(assignment)
    await db_session.flush()
    message_id = (await inbox_ids(bob))[0]

    # Read only.
    [mailbox] = (await bob.get("/mailboxes")).json()
    assert mailbox["permissions"] == ["read"]
    response = await bob.post(f"/messages/{message_id}/actions", json={"action": "archive"})
    assert (response.status_code, response.json()["error_code"]) == (403, "read_only")
    response = await bob.patch(f"/messages/{message_id}", json={"flagged": True})
    assert (response.status_code, response.json()["error_code"]) == (403, "read_only")

    # With ``act``: actions, but never sending.
    assignment.permission = AssignmentPermission.ACT
    await db_session.flush()
    [mailbox] = (await bob.get("/mailboxes")).json()
    assert mailbox["permissions"] == ["act", "read"]
    assert (
        await access.get_mailbox(db_session, bob_user.id, mailbox_id, MailboxPermission.SEND)
        is None
    )
    response = await bob.patch(f"/messages/{message_id}", json={"flagged": True, "seen": True})
    assert response.status_code == 200
    assert flag_writes == [uuid.UUID(message_id)]
    result = await act(bob, message_id, "archive")
    archive = await folder(db_session, mailbox_id, FolderRole.ARCHIVE)
    assert result["folder_ids"] == [str(archive.id)]
    [entry] = await audit_rows(db_session, AuditAction.MAIL_MOVED)
    assert entry.actor_id == bob_user.id
