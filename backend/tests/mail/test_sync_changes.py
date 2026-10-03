"""Sync engine: ``MessageChanged``, messages without a synced folder, saving credentials,
and a full Microsoft Graph sync (synthetic responses, database needed)."""

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import Text, select, type_coerce
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import MailSettings
from app.core.crypto import KeyRing, decode_key, generate_key, set_keyring
from app.mail import hooks
from app.mail.models import Folder, FolderRole, Mailbox, MailboxType, Message
from app.mail.providers.base import (
    CursorAdvanced,
    FlagsReported,
    MailboxConfig,
    MessageChanged,
    MessageFetched,
    MessageNotFoundError,
    MessageUpdated,
    ProviderCapabilities,
    RawMessage,
    RemoteFolder,
    SyncCursor,
    SyncEvent,
)
from app.mail.providers.graph import GraphProvider
from app.mail.storage import AttachmentStorage
from app.mail.sync import engine
from app.mail.sync.engine import sync_mailbox
from tests.factories import make_user
from tests.mail.conftest import load_fixture
from tests.mail.graph_helpers import (
    ARCHIVE,
    GRAPH,
    GRAPH_HOST,
    INBOX,
    NOW,
    SECURITY,
    TOKEN_URL,
    TRASH,
    Sleeps,
    batch_handler,
    delta_url,
    graph_error,
    graph_settings,
    message,
    mime,
    mime_part,
    page,
    removed,
    token_response,
)

pytestmark = pytest.mark.db

SYNC_NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def stored() -> Iterator[list[hooks.MessageStored]]:
    calls: list[hooks.MessageStored] = []

    async def record(event: hooks.MessageStored) -> None:
        calls.append(event)

    hooks.on_message_stored(record)
    yield calls
    hooks.remove_message_stored_handler(record)


@pytest.fixture(autouse=True)
def _keyring() -> Iterator[None]:
    set_keyring(KeyRing(decode_key(generate_key())))
    yield
    set_keyring(None)


@pytest.fixture(autouse=True)
def _no_events(monkeypatch: pytest.MonkeyPatch) -> None:
    async def publish(*args: Any) -> None:
        return None

    monkeypatch.setattr(engine, "publish", publish)


async def add_mailbox(session: AsyncSession, **values: Any) -> uuid.UUID:
    mailbox = Mailbox(
        type=values.pop("type", MailboxType.IMAP),
        display_name="Test",
        address="erika@example.com",
        owner_user_id=(await make_user(session)).id,
        **values,
    )
    session.add(mailbox)
    await session.commit()
    return mailbox.id


async def run(
    session: AsyncSession, mailbox_id: uuid.UUID, factory: Any, storage: AttachmentStorage
) -> engine.SyncStats | None:
    return await sync_mailbox(
        session,
        mailbox_id,
        storage=storage,
        settings=MailSettings(),
        provider_factory=factory,
        now=lambda: SYNC_NOW,
    )


async def messages(session: AsyncSession, mailbox_id: uuid.UUID) -> dict[str, list[str]]:
    """remote_ref → remote folder IDs."""
    rows = await session.scalars(
        select(Message)
        .where(Message.mailbox_id == mailbox_id)
        .options(selectinload(Message.folders))
        .execution_options(populate_existing=True)
    )
    return {m.remote_ref: sorted(f.remote_id for f in m.folders) for m in rows}


class ScriptedProvider:
    """Yields scripted events per folder; records ``load`` calls."""

    capabilities = ProviderCapabilities()

    def __init__(self, script: dict[str, list[SyncEvent]]) -> None:
        self.script = script
        self.loaded: list[str] = []
        self.config: MailboxConfig | None = None

    async def list_folders(self) -> list[RemoteFolder]:
        return [
            RemoteFolder("INBOX", "Inbox", role=FolderRole.INBOX),
            RemoteFolder("Archive", "Archive", role=FolderRole.ARCHIVE),
            RemoteFolder("Trash", "Trash", role=FolderRole.TRASH),
        ]

    async def fetch_since(
        self, folder_id: str, cursor: SyncCursor | None, *, since: datetime | None = None
    ) -> AsyncIterator[SyncEvent]:
        for event in self.script.pop(folder_id, []):
            yield event
        yield CursorAdvanced(SyncCursor({"done": True}))

    def loader(self, ref: str, folder: str, fixture: str | None = "01-plain-ascii.eml") -> Any:
        async def load() -> RawMessage:
            self.loaded.append(ref)
            if fixture is None:
                raise MessageNotFoundError()
            return RawMessage(ref, load_fixture(fixture), folder_ids=(folder,))

        return load

    async def aclose(self) -> None:
        return None


async def test_message_changed_loads_only_unknown_messages(
    db_session: AsyncSession, storage: AttachmentStorage, stored: list[hooks.MessageStored]
) -> None:
    mailbox_id = await add_mailbox(db_session)
    provider = ScriptedProvider(
        {
            "INBOX": [
                MessageFetched(
                    RawMessage("known", load_fixture("01-plain-ascii.eml"), ("INBOX",)),
                    initial=True,
                )
            ]
        }
    )
    await run(db_session, mailbox_id, lambda config: provider, storage)
    stored.clear()

    provider.script = {
        "INBOX": [
            MessageChanged(
                "known", provider.loader("known", "INBOX"), frozenset({"seen"}), ("INBOX",)
            ),
            MessageChanged("new", provider.loader("new", "INBOX"), frozenset(), ("INBOX",)),
            MessageChanged("vanished", provider.loader("vanished", "INBOX", None)),
        ]
    }
    stats = await run(db_session, mailbox_id, lambda config: provider, storage)

    assert provider.loaded == ["new", "vanished"]
    assert stats is not None and stats.stored == 1 and stats.updated == 1
    assert await messages(db_session, mailbox_id) == {"known": ["INBOX"], "new": ["INBOX"]}
    flags = await db_session.scalar(
        select(Message.flags)
        .where(Message.remote_ref == "known")
        .execution_options(populate_existing=True)
    )
    assert flags == ["seen"]
    # Newly arrived, not backfill: the processing pipeline gives it priority.
    assert [(call.backfill) for call in stored] == [False]


async def test_messages_moved_out_of_synced_folders_are_deleted(
    db_session: AsyncSession, storage: AttachmentStorage, stored: list[hooks.MessageStored]
) -> None:
    mailbox_id = await add_mailbox(db_session)
    raw = load_fixture("01-plain-ascii.eml")
    provider = ScriptedProvider(
        {
            "INBOX": [
                MessageFetched(RawMessage("a", raw, ("INBOX",)), initial=True),
                MessageFetched(RawMessage("b", raw, ("INBOX",)), initial=True),
            ]
        }
    )
    await run(db_session, mailbox_id, lambda config: provider, storage)

    provider.script = {
        "INBOX": [
            # Moved to a synced folder: kept, folder updated, not processed again.
            MessageUpdated("a", folder_ids=("Archive",)),
            # Moved to the trash (not synced): deleted like on IMAP.
            MessageUpdated("b", folder_ids=("Trash",)),
        ]
    }
    stored.clear()
    stats = await run(db_session, mailbox_id, lambda config: provider, storage)

    assert await messages(db_session, mailbox_id) == {"a": ["Archive"]}
    assert stats is not None and stats.deleted == 1
    assert stored == []


async def test_providers_can_save_new_credentials(
    db_session: AsyncSession, storage: AttachmentStorage
) -> None:
    mailbox_id = await add_mailbox(db_session, credentials={"refresh_token": "old"})

    class Rotating(ScriptedProvider):
        async def list_folders(self) -> list[RemoteFolder]:
            assert self.config is not None and self.config.save_credentials is not None
            await self.config.save_credentials({"refresh_token": "new"})
            return []

    provider = Rotating({})

    def factory(config: MailboxConfig) -> Rotating:
        provider.config = config
        assert config.credentials == {"refresh_token": "old"}
        return provider

    await run(db_session, mailbox_id, factory, storage)

    mailbox = await db_session.get(Mailbox, mailbox_id, populate_existing=True)
    assert mailbox is not None and mailbox.credentials == {"refresh_token": "new"}
    stored_value = await db_session.scalar(
        select(type_coerce(Mailbox.__table__.c.credentials, Text)).where(Mailbox.id == mailbox_id)
    )
    # Encrypted at rest.
    assert stored_value is not None and "new" not in stored_value


# -- Microsoft Graph end to end -------------------------------------------------------------


class GraphServer:
    """Scripted delta responses per folder for consecutive syncs."""

    def __init__(self, router: respx.MockRouter) -> None:
        self.round = 0
        self.deltas: dict[str, list[httpx.Response]] = {}
        self.messages: dict[str, httpx.Response] = {}
        router.post(TOKEN_URL).mock(return_value=token_response())
        router.post(f"{GRAPH}/$batch").mock(side_effect=batch_handler(self._batch))
        router.get(host=GRAPH_HOST, path="/v1.0/me/mailFolders").respond(
            json={
                "value": [
                    {"id": INBOX, "displayName": "Posteingang", "childFolderCount": 0},
                    {"id": ARCHIVE, "displayName": "Archiv", "childFolderCount": 0},
                    {"id": TRASH, "displayName": "Gelöschte Elemente", "childFolderCount": 0},
                ]
            }
        )
        router.get(host=GRAPH_HOST, path__regex=r"/v1\.0/me/mailFolders/[^/]+/messages/delta").mock(
            side_effect=self._delta
        )
        router.get(host=GRAPH_HOST, path__regex=r"/v1\.0/me/messages/[^/]+$").mock(
            side_effect=self._message
        )
        router.get(host=GRAPH_HOST, path__regex=r"/v1\.0/me/messages/[^/]+/\$value").mock(
            side_effect=lambda request: httpx.Response(200, content=mime())
        )

    def _batch(self, item: dict[str, Any]) -> dict[str, Any]:
        roles = {"inbox": INBOX, "archive": ARCHIVE, "deleteditems": TRASH}
        name = item["url"].split("/")[3].split("?")[0] if "mailFolders" in item["url"] else ""
        if name in roles:
            return {"id": item["id"], "status": 200, "headers": {}, "body": {"id": roles[name]}}
        if "mailFolders" in item["url"]:
            return {"id": item["id"], "status": 404, "headers": {}, "body": {}}
        return mime_part(item["id"], mime())

    def _delta(self, request: httpx.Request) -> httpx.Response:
        folder = request.url.path.split("/")[4]
        queue = self.deltas.get(folder) or []
        if queue:
            return queue.pop(0)
        return page([], delta=delta_url(folder, f"r{self.round}"))

    def _message(self, request: httpx.Request) -> httpx.Response:
        ref = request.url.path.rsplit("/", 1)[1]
        return self.messages.get(ref) or graph_error(404, "ErrorItemNotFound")


async def test_graph_mailbox_sync_end_to_end(
    db_session: AsyncSession, storage: AttachmentStorage, stored: list[hooks.MessageStored]
) -> None:
    mailbox_id = await add_mailbox(
        db_session,
        type=MailboxType.GRAPH,
        provider_settings={"auth": "delegated", "user": "me"},
        credentials={"refresh_token": "refresh-1", "access_token": "x", "expires_at": 0},
    )

    def factory(config: MailboxConfig) -> GraphProvider:
        return GraphProvider(
            config,
            settings=graph_settings(),
            mail_settings=MailSettings(),
            security=SECURITY,
            sleep=Sleeps(),
            clock=lambda: NOW,
        )

    with respx.mock(assert_all_called=False) as router:
        server = GraphServer(router)
        server.deltas[INBOX] = [
            page([message("m1"), message("m2")], delta=delta_url(INBOX, "1")),
        ]
        stats = await run(db_session, mailbox_id, factory, storage)
        assert stats is not None and stats.stored == 2 and stats.folders == 2
        assert await messages(db_session, mailbox_id) == {"m1": [INBOX], "m2": [INBOX]}
        assert all(call.backfill for call in stored)
        stored.clear()

        # The refreshed token was stored (encrypted) right away.
        mailbox = await db_session.get(Mailbox, mailbox_id, populate_existing=True)
        assert mailbox is not None and mailbox.credentials is not None
        assert mailbox.credentials["refresh_token"] == "refresh-2"

        # m1 moved to the archive, a new mail arrives, m2 is read.
        server.round = 1
        server.messages["m1"] = httpx.Response(200, json=message("m1", ARCHIVE))
        server.deltas[INBOX] = [
            page(
                [removed("m1"), message("m2", read=True), message("m3")],
                delta=delta_url(INBOX, "2"),
            )
        ]
        server.deltas[ARCHIVE] = [page([message("m1", ARCHIVE)], delta=delta_url(ARCHIVE, "2"))]
        stats = await run(db_session, mailbox_id, factory, storage)

        assert await messages(db_session, mailbox_id) == {
            "m1": [ARCHIVE],
            "m2": [INBOX],
            "m3": [INBOX],
        }
        # Only m3 is new; the moved message is not processed again.
        assert len(stored) == 1
        assert stats is not None and stats.stored == 1

        # m1 deleted (moved to Deleted Items, which is not synced).
        server.messages["m1"] = httpx.Response(200, json=message("m1", TRASH))
        server.deltas[ARCHIVE] = [page([removed("m1")], delta=delta_url(ARCHIVE, "3"))]
        await run(db_session, mailbox_id, factory, storage)
        assert set(await messages(db_session, mailbox_id)) == {"m2", "m3"}

        # An expired delta token re-imports the folder.
        server.deltas[INBOX] = [
            graph_error(410, "SyncStateNotFound"),
            page([message("m2"), message("m3")], delta=delta_url(INBOX, "4")),
        ]
        await run(db_session, mailbox_id, factory, storage)
        assert set(await messages(db_session, mailbox_id)) == {"m2", "m3"}

    folders = await db_session.scalars(select(Folder).where(Folder.mailbox_id == mailbox_id))
    roles = {f.remote_id: f.role for f in folders}
    assert roles == {INBOX: FolderRole.INBOX, ARCHIVE: FolderRole.ARCHIVE, TRASH: FolderRole.TRASH}


async def test_flags_reported_in_bulk_write_only_differences(
    db_session: AsyncSession, storage: AttachmentStorage
) -> None:
    """#147: IMAP without CONDSTORE reports the flags of many messages at once; they are
    compared in the database and only changed rows are written."""
    raw = load_fixture("01-plain-ascii.eml")
    mailbox_ids = [await add_mailbox(db_session), await add_mailbox(db_session)]
    for mailbox_id in mailbox_ids:
        provider = ScriptedProvider(
            {
                "INBOX": [
                    MessageFetched(RawMessage(ref, raw, ("INBOX",), flags), initial=True)
                    for ref, flags in (
                        ("a", frozenset()),
                        ("b", frozenset({"seen"})),
                        ("c", frozenset({"seen", "flagged"})),
                    )
                ]
            }
        )
        await run(db_session, mailbox_id, lambda config, p=provider: p, storage)
    mailbox_id, other_id = mailbox_ids

    async def flags(mailbox: uuid.UUID) -> dict[str, list[str]]:
        rows = await db_session.execute(
            select(Message.remote_ref, Message.flags)
            .where(Message.mailbox_id == mailbox)
            .execution_options(populate_existing=True)
        )
        return dict(rows.tuples().all())

    before = await flags(other_id)
    provider = ScriptedProvider(
        {
            "INBOX": [
                FlagsReported({"a": frozenset({"seen"}), "b": frozenset({"seen"})}),
                # A second report in the same transaction, plus an unknown reference.
                FlagsReported({"c": frozenset({"flagged", "seen"}), "gone": frozenset()}),
                FlagsReported({"c": frozenset({"Wichtig"})}),
            ]
        }
    )
    stats = await run(db_session, mailbox_id, lambda config: provider, storage)

    assert stats is not None and stats.updated == 2
    assert await flags(mailbox_id) == {"a": ["seen"], "b": ["seen"], "c": ["Wichtig"]}
    assert await flags(other_id) == before
