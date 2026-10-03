"""Mail actions (#148) with the real providers: archive, trash and undo run against the
synthetic servers (Dovecot for IMAP, respx for Microsoft Graph and Gmail), the stored
message follows, and the real sync engine confirms the result in PostgreSQL afterwards.

All mailboxes, addresses and messages are invented (example.org / example.com).
"""

import json
import re
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import audit
from app.audit.events import AuditAction
from app.core.config import GmailSettings, MailSettings
from app.core.crypto import KeyRing, decode_key, generate_key, set_keyring
from app.mail.actions import ActionOutcome, MessageAction, NoTargetFolderError, move_message
from app.mail.models import Folder, FolderRole, Mailbox, MailboxType, Message
from app.mail.providers.base import (
    MailboxConfig,
    MailProvider,
    MessageFetched,
    MessageNotFoundError,
)
from app.mail.providers.gmail import ALL_MAIL, GmailProvider
from app.mail.providers.graph import GraphProvider
from app.mail.providers.imap import ImapProvider
from app.mail.storage import AttachmentStorage
from app.mail.sync.engine import sync_mailbox
from tests.audit.conftest import audit_rows
from tests.factories import make_user
from tests.mail.gmail_server import GmailServer, StaticTokens
from tests.mail.graph_helpers import (
    ARCHIVE,
    GRAPH_HOST,
    INBOX,
    NOW,
    SECURITY,
    TRASH,
    Sleeps,
    credentials,
    delta_url,
    graph_error,
    graph_settings,
    message,
    mime,
    mime_part,
    removed,
)
from tests.mail.imap_server import INSECURE, TestAccount
from tests.mail.imap_server import message as imap_message
from tests.mail.test_gmail_provider import RECENT, mail, no_sleep
from tests.mail.test_sync import NOW as SYNC_NOW
from tests.mail.test_sync import mid

pytestmark = pytest.mark.db

Factory = Callable[[MailboxConfig], MailProvider]


@pytest.fixture(autouse=True)
def keyring() -> Iterator[None]:
    # Credentials are encrypted (EncryptedJSON).
    set_keyring(KeyRing(decode_key(generate_key())))
    yield
    set_keyring(None)


# -- shared helpers -------------------------------------------------------------------------


async def add_mailbox(session: AsyncSession, **values: Any) -> Mailbox:
    mailbox = Mailbox(
        display_name="Test",
        owner_user_id=(await make_user(session)).id,
        **values,
    )
    session.add(mailbox)
    # Committed like a real mailbox: a rollback in the engine must not remove it.
    await session.commit()
    return mailbox


async def sync(
    session: AsyncSession,
    mailbox: Mailbox,
    storage: AttachmentStorage,
    factory: Factory,
    now: datetime | None = None,
) -> None:
    stats = await sync_mailbox(
        session,
        mid(mailbox),
        storage=storage,
        settings=MailSettings(),
        provider_factory=factory,
        **({"now": lambda: now} if now is not None else {}),
    )
    assert stats is not None and stats.failed_folders == 0


async def messages(session: AsyncSession, mailbox: Mailbox) -> list[Message]:
    rows = await session.scalars(
        select(Message)
        .where(Message.mailbox_id == mid(mailbox))
        .options(selectinload(Message.folders))
        .execution_options(populate_existing=True)
    )
    return list(rows)


async def only_message(session: AsyncSession, mailbox: Mailbox) -> Message:
    (stored,) = await messages(session, mailbox)
    return stored


def remote_ids(stored: Message) -> set[str]:
    return {folder.remote_id for folder in stored.folders}


async def folder(session: AsyncSession, mailbox: Mailbox, remote_id: str) -> Folder:
    found = await session.scalar(
        select(Folder).where(Folder.mailbox_id == mid(mailbox), Folder.remote_id == remote_id)
    )
    assert found is not None
    return found


async def act(
    session: AsyncSession,
    mailbox: Mailbox,
    stored: Message,
    action: MessageAction,
    factory: Factory,
    *,
    folder_id: uuid.UUID | None = None,
) -> ActionOutcome:
    assert mailbox.owner_user_id is not None
    outcome = await move_message(
        session,
        stored.id,
        mailbox,
        action,
        folder_id=folder_id,
        actor=audit.Actor.user(mailbox.owner_user_id),
        provider_factory=factory,
    )
    await session.commit()
    return outcome


async def assert_moved_audit(
    session: AsyncSession, mailbox: Mailbox, stored: Message, action: str, target: Folder
) -> None:
    (row,) = await audit_rows(session, AuditAction.MAIL_MOVED)
    assert (row.actor_kind, row.actor_id) == ("user", mailbox.owner_user_id)
    assert (row.target_type, row.target_id) == ("mailbox", str(mailbox.id))
    assert row.details == {
        "message_id": str(stored.id),
        "action": action,
        "folder_id": str(target.id),
    }


async def assert_unchanged_after_rollback(
    session: AsyncSession, mailbox: Mailbox, ref: str, folders: set[str]
) -> None:
    await session.rollback()
    stored = await only_message(session, mailbox)
    assert stored.remote_ref == ref
    assert remote_ids(stored) == folders
    assert await audit_rows(session, AuditAction.MAIL_MOVED) == []


# -- IMAP (Dovecot) -------------------------------------------------------------------------


def imap_factory(config: MailboxConfig) -> ImapProvider:
    return ImapProvider(config, mail_settings=INSECURE)


async def imap_mailbox(session: AsyncSession, account: TestAccount) -> Mailbox:
    config = account.config()
    return await add_mailbox(
        session,
        type=MailboxType.IMAP,
        address=account.address,
        provider_settings=config.settings,
        credentials=config.credentials,
    )


async def imap_refs(account: TestAccount, remote_folder: str) -> list[str]:
    """References of the messages in ``remote_folder``, as the server reports them now."""
    provider = account.provider()
    try:
        events = [event async for event in provider.fetch_since(remote_folder, None)]
    finally:
        await provider.aclose()
    return [e.message.remote_ref for e in events if isinstance(e, MessageFetched)]


@pytest.fixture
async def imap(
    db_session: AsyncSession, imap_account: TestAccount, storage: AttachmentStorage
) -> Mailbox:
    """A synced IMAP mailbox with an "Archive" folder and one message in the inbox."""
    await imap_account.run("CREATE", "Archive")
    await imap_account.append(imap_message(1))
    mailbox = await imap_mailbox(db_session, imap_account)
    await sync(db_session, mailbox, storage, imap_factory)
    return mailbox


@pytest.mark.imap
async def test_imap_archive_moves_the_message_and_the_next_sync_agrees(
    db_session: AsyncSession, imap: Mailbox, imap_account: TestAccount, storage: AttachmentStorage
) -> None:
    stored = await only_message(db_session, imap)
    assert remote_ids(stored) == {"INBOX"}
    archive = await folder(db_session, imap, "Archive")
    assert archive.role == FolderRole.ARCHIVE

    outcome = await act(db_session, imap, stored, MessageAction.ARCHIVE, imap_factory)

    assert outcome.moved and outcome.target.id == archive.id
    assert await imap_refs(imap_account, "INBOX") == []
    (archived_ref,) = await imap_refs(imap_account, "Archive")
    stored = await only_message(db_session, imap)
    assert stored.remote_ref == archived_ref  # new UID in the archive folder
    assert remote_ids(stored) == {"Archive"}
    await assert_moved_audit(db_session, imap, stored, "archive", archive)

    await sync(db_session, imap, storage, imap_factory)

    synced = await only_message(db_session, imap)
    assert (synced.id, synced.remote_ref) == (stored.id, archived_ref)
    assert remote_ids(synced) == {"Archive"}


@pytest.mark.imap
async def test_imap_trash_moves_the_message_to_the_trash_folder(
    db_session: AsyncSession, imap: Mailbox, imap_account: TestAccount
) -> None:
    stored = await only_message(db_session, imap)
    trash = await folder(db_session, imap, "Trash")
    assert trash.role == FolderRole.TRASH

    outcome = await act(db_session, imap, stored, MessageAction.TRASH, imap_factory)

    assert outcome.target.id == trash.id
    assert await imap_refs(imap_account, "INBOX") == []
    (trashed_ref,) = await imap_refs(imap_account, "Trash")
    stored = await only_message(db_session, imap)
    assert stored.remote_ref == trashed_ref
    assert remote_ids(stored) == {"Trash"}
    await assert_moved_audit(db_session, imap, stored, "trash", trash)


@pytest.mark.imap
async def test_imap_undo_moves_the_archived_message_back_to_the_inbox(
    db_session: AsyncSession, imap: Mailbox, imap_account: TestAccount, storage: AttachmentStorage
) -> None:
    stored = await only_message(db_session, imap)
    inbox = await folder(db_session, imap, "INBOX")
    archived = await act(db_session, imap, stored, MessageAction.ARCHIVE, imap_factory)
    assert archived.undo_folder is not None and archived.undo_folder.id == inbox.id

    await act(db_session, imap, stored, MessageAction.MOVE, imap_factory, folder_id=inbox.id)

    assert await imap_refs(imap_account, "Archive") == []
    (inbox_ref,) = await imap_refs(imap_account, "INBOX")
    await sync(db_session, imap, storage, imap_factory)
    synced = await only_message(db_session, imap)
    assert (synced.id, synced.remote_ref) == (stored.id, inbox_ref)
    assert remote_ids(synced) == {"INBOX"}


@pytest.mark.imap
async def test_imap_mailbox_without_archive_folder_cannot_archive(
    db_session: AsyncSession, imap_account: TestAccount, storage: AttachmentStorage
) -> None:
    await imap_account.append(imap_message(1))
    mailbox = await imap_mailbox(db_session, imap_account)
    await sync(db_session, mailbox, storage, imap_factory)
    stored = await only_message(db_session, mailbox)
    ref = stored.remote_ref

    with pytest.raises(NoTargetFolderError) as error:
        await act(db_session, mailbox, stored, MessageAction.ARCHIVE, imap_factory)

    assert error.value.code == "no_archive_folder"
    assert await imap_refs(imap_account, "INBOX") == [ref]
    await assert_unchanged_after_rollback(db_session, mailbox, ref, {"INBOX"})


@pytest.mark.imap
async def test_imap_message_deleted_on_the_server_changes_nothing(
    db_session: AsyncSession, imap: Mailbox, imap_account: TestAccount
) -> None:
    stored = await only_message(db_session, imap)
    ref = stored.remote_ref
    await imap_account.run("STORE", "1:*", "+FLAGS.SILENT", "(\\Deleted)", folder="INBOX")
    await imap_account.run("EXPUNGE")

    with pytest.raises(MessageNotFoundError):
        await act(db_session, imap, stored, MessageAction.ARCHIVE, imap_factory)

    await assert_unchanged_after_rollback(db_session, imap, ref, {"INBOX"})
    assert await imap_refs(imap_account, "Archive") == []


# -- Microsoft Graph (respx) ----------------------------------------------------------------


class GraphMailbox:
    """A stateful synthetic Exchange mailbox behind Microsoft Graph: well-known folders,
    messages with immutable IDs (they survive moves) and a delta query per folder. The
    delta token is a position in the change log."""

    def __init__(self, *, archive: bool = True) -> None:
        self.folders = {INBOX: ("Inbox", "inbox"), TRASH: ("Deleted Items", "deleteditems")}
        if archive:
            self.folders[ARCHIVE] = ("Archive", "archive")
        self.items: dict[str, dict[str, Any]] = {}
        # (message ID, folder it left, folder it entered)
        self.log: list[tuple[str, str | None, str | None]] = []

    def add(self, message_id: str, folder_id: str = INBOX) -> str:
        self.items[message_id] = message(message_id, folder_id)
        self.log.append((message_id, None, folder_id))
        return message_id

    def delete(self, message_id: str) -> None:
        item = self.items.pop(message_id)
        self.log.append((message_id, item["parentFolderId"], None))

    def folder_of(self, message_id: str) -> str | None:
        item = self.items.get(message_id)
        return None if item is None else str(item["parentFolderId"])

    def install(self, router: respx.MockRouter) -> None:
        router.route(host=GRAPH_HOST).mock(side_effect=self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1.0/$batch" and request.method == "POST":
            return self._batch(request)
        path = request.url.path.removeprefix("/v1.0/me")
        if path == "/mailFolders" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"id": folder_id, "displayName": name, "childFolderCount": 0}
                        for folder_id, (name, _) in self.folders.items()
                    ]
                },
            )
        if match := re.fullmatch(r"/mailFolders/([^/]+)/messages/delta", path):
            return self._delta(match.group(1), request.url.params.get("$deltatoken"))
        if (match := re.fullmatch(r"/messages/([^/]+)", path)) and request.method == "GET":
            item = self.items.get(match.group(1))
            if item is None:
                return graph_error(404, "ErrorItemNotFound")
            return httpx.Response(200, json=item)
        if (match := re.fullmatch(r"/messages/([^/]+)/move", path)) and request.method == "POST":
            item = self.items.get(match.group(1))
            if item is None:
                return graph_error(404, "ErrorItemNotFound")
            destination = str(json.loads(request.content)["destinationId"])
            if destination not in self.folders:
                return graph_error(400, "ErrorInvalidIdMalformed")
            self.log.append((item["id"], item["parentFolderId"], destination))
            item["parentFolderId"] = destination
            return httpx.Response(201, json=item)
        return graph_error(404, "ErrorInvalidRequest")

    def _delta(self, folder_id: str, token: str | None) -> httpx.Response:
        if folder_id not in self.folders:
            return graph_error(404, "ErrorItemNotFound")
        if token is None:
            value = [item for item in self.items.values() if item["parentFolderId"] == folder_id]
        else:
            touched = dict.fromkeys(
                message_id
                for message_id, left, entered in self.log[int(token) :]
                if folder_id in (left, entered)
            )
            value = [
                self.items[m] if self.folder_of(m) == folder_id else removed(m) for m in touched
            ]
        return httpx.Response(
            200,
            json={"value": value, "@odata.deltaLink": delta_url(folder_id, str(len(self.log)))},
        )

    def _batch(self, request: httpx.Request) -> httpx.Response:
        responses = []
        for part in json.loads(request.content)["requests"]:
            url = str(part["url"])
            if match := re.fullmatch(r"/me/mailFolders/(\w+)\?\$select=id", url):
                found = [f for f, (_, name) in self.folders.items() if name == match.group(1)]
                if found:
                    responses.append(
                        {"id": part["id"], "status": 200, "headers": {}, "body": {"id": found[0]}}
                    )
                else:
                    responses.append(
                        {
                            "id": part["id"],
                            "status": 404,
                            "headers": {},
                            "body": {"error": {"code": "ErrorFolderNotFound"}},
                        }
                    )
            elif (match := re.fullmatch(r"/me/messages/([^/]+)/\$value", url)) and (
                match.group(1) in self.items
            ):
                responses.append(mime_part(part["id"], mime()))
            else:
                responses.append({"id": part["id"], "status": 404, "headers": {}, "body": {}})
        return httpx.Response(200, json={"responses": responses})


def graph_factory(config: MailboxConfig) -> GraphProvider:
    return GraphProvider(
        config,
        settings=graph_settings(),
        mail_settings=MailSettings(),
        security=SECURITY,
        sleep=Sleeps(),
        clock=lambda: NOW,
    )


@pytest.fixture
def graph_server() -> Iterator[GraphMailbox]:
    server = GraphMailbox()
    with respx.mock(assert_all_called=False) as router:
        server.install(router)
        yield server


async def graph_mailbox(session: AsyncSession) -> Mailbox:
    return await add_mailbox(
        session,
        type=MailboxType.GRAPH,
        address="erika@example.com",
        provider_settings={"auth": "delegated", "user": "me"},
        credentials=credentials(),
    )


@pytest.fixture
async def graph(
    db_session: AsyncSession, graph_server: GraphMailbox, storage: AttachmentStorage
) -> Mailbox:
    """A synced Microsoft 365 mailbox with one message in the inbox."""
    graph_server.add("msg-1")
    mailbox = await graph_mailbox(db_session)
    await sync(db_session, mailbox, storage, graph_factory, SYNC_NOW)
    return mailbox


async def test_graph_archive_moves_the_message_and_the_next_sync_agrees(
    db_session: AsyncSession, graph: Mailbox, graph_server: GraphMailbox, storage: AttachmentStorage
) -> None:
    stored = await only_message(db_session, graph)
    assert (stored.remote_ref, remote_ids(stored)) == ("msg-1", {INBOX})
    archive = await folder(db_session, graph, ARCHIVE)

    outcome = await act(db_session, graph, stored, MessageAction.ARCHIVE, graph_factory)

    assert outcome.moved and outcome.target.id == archive.id
    assert graph_server.folder_of("msg-1") == ARCHIVE
    stored = await only_message(db_session, graph)
    # Immutable IDs: the reference survives the move.
    assert (stored.remote_ref, remote_ids(stored)) == ("msg-1", {ARCHIVE})
    await assert_moved_audit(db_session, graph, stored, "archive", archive)

    await sync(db_session, graph, storage, graph_factory, SYNC_NOW)

    synced = await only_message(db_session, graph)
    assert (synced.id, synced.remote_ref, remote_ids(synced)) == (stored.id, "msg-1", {ARCHIVE})


async def test_graph_trash_moves_the_message_to_deleted_items(
    db_session: AsyncSession, graph: Mailbox, graph_server: GraphMailbox
) -> None:
    stored = await only_message(db_session, graph)
    trash = await folder(db_session, graph, TRASH)
    assert trash.role == FolderRole.TRASH

    await act(db_session, graph, stored, MessageAction.TRASH, graph_factory)

    assert graph_server.folder_of("msg-1") == TRASH
    stored = await only_message(db_session, graph)
    assert remote_ids(stored) == {TRASH}
    await assert_moved_audit(db_session, graph, stored, "trash", trash)


async def test_graph_undo_moves_the_archived_message_back_to_the_inbox(
    db_session: AsyncSession, graph: Mailbox, graph_server: GraphMailbox, storage: AttachmentStorage
) -> None:
    stored = await only_message(db_session, graph)
    inbox = await folder(db_session, graph, INBOX)
    archived = await act(db_session, graph, stored, MessageAction.ARCHIVE, graph_factory)
    await sync(db_session, graph, storage, graph_factory, SYNC_NOW)
    assert archived.undo_folder is not None and archived.undo_folder.id == inbox.id

    await act(db_session, graph, stored, MessageAction.MOVE, graph_factory, folder_id=inbox.id)

    assert graph_server.folder_of("msg-1") == INBOX
    await sync(db_session, graph, storage, graph_factory, SYNC_NOW)
    synced = await only_message(db_session, graph)
    assert (synced.id, synced.remote_ref, remote_ids(synced)) == (stored.id, "msg-1", {INBOX})


async def test_graph_mailbox_without_archive_folder_cannot_archive(
    db_session: AsyncSession, storage: AttachmentStorage
) -> None:
    server = GraphMailbox(archive=False)
    server.add("msg-1")
    with respx.mock(assert_all_called=False) as router:
        server.install(router)
        mailbox = await graph_mailbox(db_session)
        await sync(db_session, mailbox, storage, graph_factory, SYNC_NOW)
        stored = await only_message(db_session, mailbox)

        with pytest.raises(NoTargetFolderError) as error:
            await act(db_session, mailbox, stored, MessageAction.ARCHIVE, graph_factory)

    assert error.value.code == "no_archive_folder"
    assert server.folder_of("msg-1") == INBOX
    await assert_unchanged_after_rollback(db_session, mailbox, "msg-1", {INBOX})


async def test_graph_message_deleted_on_the_server_changes_nothing(
    db_session: AsyncSession, graph: Mailbox, graph_server: GraphMailbox
) -> None:
    stored = await only_message(db_session, graph)
    graph_server.delete("msg-1")

    with pytest.raises(MessageNotFoundError):
        await act(db_session, graph, stored, MessageAction.TRASH, graph_factory)

    await assert_unchanged_after_rollback(db_session, graph, "msg-1", {INBOX})


# -- Gmail (respx) --------------------------------------------------------------------------


def gmail_factory(config: MailboxConfig) -> GmailProvider:
    return GmailProvider(
        config,
        settings=GmailSettings(),
        mail_settings=MailSettings(),
        token_source=StaticTokens(),
        sleep=no_sleep,
    )


@pytest.fixture
def gmail_server() -> Iterator[GmailServer]:
    server = GmailServer()
    with respx.mock(assert_all_called=False) as router:
        server.install(router)
        yield server


@pytest.fixture
async def gmail(
    db_session: AsyncSession, gmail_server: GmailServer, storage: AttachmentStorage
) -> Mailbox:
    """A synced Gmail mailbox with one message in the inbox (no archive folder)."""
    gmail_server.add_message(mail(1), ["INBOX", "UNREAD"], internal_date=RECENT)
    mailbox = await add_mailbox(
        db_session,
        type=MailboxType.GMAIL,
        address=gmail_server.address,
        provider_settings={"auth": "oauth"},
        credentials={"refresh_token": "test-refresh-token"},
    )
    await sync(db_session, mailbox, storage, gmail_factory, SYNC_NOW)
    return mailbox


def gmail_labels(server: GmailServer, remote_ref: str) -> set[str]:
    return set(server.messages[remote_ref].label_ids)


async def test_gmail_archive_removes_the_inbox_label_and_the_next_sync_agrees(
    db_session: AsyncSession, gmail: Mailbox, gmail_server: GmailServer, storage: AttachmentStorage
) -> None:
    stored = await only_message(db_session, gmail)
    ref = stored.remote_ref
    assert remote_ids(stored) == {"INBOX", ALL_MAIL}
    all_mail = await folder(db_session, gmail, ALL_MAIL)

    outcome = await act(db_session, gmail, stored, MessageAction.ARCHIVE, gmail_factory)

    # No archive folder: "All Mail" is the target.
    assert outcome.moved and outcome.target.id == all_mail.id
    assert gmail_labels(gmail_server, ref) == {"UNREAD"}
    stored = await only_message(db_session, gmail)
    assert (stored.remote_ref, remote_ids(stored)) == (ref, {ALL_MAIL})
    await assert_moved_audit(db_session, gmail, stored, "archive", all_mail)

    await sync(db_session, gmail, storage, gmail_factory, SYNC_NOW)

    synced = await only_message(db_session, gmail)
    assert (synced.id, synced.remote_ref, remote_ids(synced)) == (stored.id, ref, {ALL_MAIL})


async def test_gmail_trash_sets_the_trash_label(
    db_session: AsyncSession, gmail: Mailbox, gmail_server: GmailServer
) -> None:
    stored = await only_message(db_session, gmail)
    ref = stored.remote_ref
    trash = await folder(db_session, gmail, "TRASH")

    await act(db_session, gmail, stored, MessageAction.TRASH, gmail_factory)

    labels = gmail_labels(gmail_server, ref)
    assert "TRASH" in labels and "INBOX" not in labels
    stored = await only_message(db_session, gmail)
    assert (stored.remote_ref, remote_ids(stored)) == (ref, {"TRASH"})
    await assert_moved_audit(db_session, gmail, stored, "trash", trash)


async def test_gmail_undo_puts_the_archived_message_back_into_the_inbox(
    db_session: AsyncSession, gmail: Mailbox, gmail_server: GmailServer, storage: AttachmentStorage
) -> None:
    stored = await only_message(db_session, gmail)
    ref = stored.remote_ref
    inbox = await folder(db_session, gmail, "INBOX")
    archived = await act(db_session, gmail, stored, MessageAction.ARCHIVE, gmail_factory)
    await sync(db_session, gmail, storage, gmail_factory, SYNC_NOW)
    assert archived.undo_folder is not None and archived.undo_folder.id == inbox.id

    await act(db_session, gmail, stored, MessageAction.MOVE, gmail_factory, folder_id=inbox.id)

    assert "INBOX" in gmail_labels(gmail_server, ref)
    await sync(db_session, gmail, storage, gmail_factory, SYNC_NOW)
    synced = await only_message(db_session, gmail)
    assert (synced.id, remote_ids(synced)) == (stored.id, {"INBOX", ALL_MAIL})


async def test_gmail_message_deleted_on_the_server_changes_nothing(
    db_session: AsyncSession, gmail: Mailbox, gmail_server: GmailServer
) -> None:
    stored = await only_message(db_session, gmail)
    ref = stored.remote_ref
    gmail_server.delete_message(ref)

    with pytest.raises(MessageNotFoundError):
        await act(db_session, gmail, stored, MessageAction.ARCHIVE, gmail_factory)

    await assert_unchanged_after_rollback(db_session, gmail, ref, {"INBOX", ALL_MAIL})
