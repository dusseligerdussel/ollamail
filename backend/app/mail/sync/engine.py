"""Synchronising one mailbox: folders, initial import and incremental changes.

``sync_mailbox`` is provider-independent: it drives a ``MailProvider`` (from the registry)
and stores through ``app.mail.service``. Per folder it runs ``fetch_since`` with the stored
cursor and applies the events:

* ``MessageFetched``  → ``store_message`` (new messages are reported to
  ``app.mail.hooks.message_stored`` after the commit),
* ``MessageUpdated``  → flags/folders of the stored message; a message that is in no
  synced folder any more (e.g. moved to the trash) is deleted,
* ``MessageChanged``  → like ``MessageUpdated`` for known messages, otherwise the source
  is loaded and stored like ``MessageFetched``,
* ``MessageDeleted``  → ``delete_messages`` (hard delete incl. attachment files),
* ``FlagsReported``   → flags of many stored messages, compared in bulk in a temporary
  table; only rows whose flags differ are written (IMAP without CONDSTORE, #147),
* ``CursorAdvanced``  → everything above plus the new cursor is committed in one
  transaction. A sync that breaks off resumes at the last committed cursor.

An invalid cursor (``CursorInvalidError``, e.g. a new IMAP ``UIDVALIDITY``) deletes the
folder's messages and imports the folder again. Folders that disappeared on the server are
deleted with their messages.

Providers with a mailbox-wide change log (``capabilities.mailbox_cursor``, e.g. Gmail) are
synced with one ``fetch_since(MAILBOX_SCOPE, ...)`` per run; the cursor lives in the
mailbox-level ``SyncState``. Only messages in at least one selected folder are stored; a
stored message that leaves the last selected folder is deleted. Their references are
stable, so an invalid cursor does not wipe the mailbox: the time window is imported again
(known messages are only updated) and stored messages of the window that the server no
longer returned are deleted afterwards (``_sync_scope`` with ``reconcile``).

Status: ``SyncState.last_error``/``last_synced_at`` per folder; errors that concern the
whole mailbox (authentication, connection, configuration) go to the mailbox-level row
(``folder_id IS NULL``), which also records the last complete sync. Only error codes are
stored, never server messages. The owner receives ``mailbox.sync`` events (``progress``
while new messages arrive, then ``done`` or ``failed``).
"""

import json
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, exists, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased, selectinload

from app.core.config import MailSettings
from app.core.events import Event, publish
from app.core.ids import uuid7
from app.core.logging import get_logger
from app.mail.access import publish_to_readers
from app.mail.hooks import message_stored
from app.mail.models import Folder, FolderRole, Mailbox, Message, SyncState
from app.mail.models import message_folders as message_folders_table
from app.mail.providers.base import (
    MAILBOX_SCOPE,
    AuthenticationError,
    ConfigurationError,
    ConnectionFailedError,
    CursorAdvanced,
    CursorInvalidError,
    FlagsReported,
    MailboxConfig,
    MailProvider,
    MessageChanged,
    MessageDeleted,
    MessageFetched,
    MessageNotFoundError,
    MessageUpdated,
    ProviderError,
    RawMessage,
    RemoteFolder,
    SaveCredentials,
    SyncCursor,
)
from app.mail.providers.registry import ProviderFactory, registry
from app.mail.schemas import SyncSettings
from app.mail.service import delete_messages, store_message
from app.mail.storage import AttachmentStorage

log = get_logger(__name__)

# Errors that affect every folder: the sync of the mailbox stops.
MAILBOX_ERRORS = (AuthenticationError, ConnectionFailedError, ConfigurationError)
_UPDATE_CHUNK = 500

# ``FlagsReported`` (#147): reported flags, one row per message, dropped with the commit.
_FLAG_SCAN_TABLE = (
    "CREATE TEMPORARY TABLE IF NOT EXISTS mail_flag_scan"
    " (remote_ref text PRIMARY KEY, flags text[] NOT NULL) ON COMMIT DROP"
)
# From one JSON object ``{remote_ref: [flag, ...]}``; the lists are sorted like
# ``Message.flags``, so comparing arrays compares sets.
_FLAG_SCAN_LOAD = (
    "INSERT INTO mail_flag_scan (remote_ref, flags)"
    " SELECT key, ARRAY(SELECT jsonb_array_elements_text(value))"
    " FROM jsonb_each(CAST(:flags AS jsonb))"
)
_FLAG_SCAN_APPLY = (
    "UPDATE mail_messages AS m SET flags = s.flags, updated_at = now()"
    " FROM mail_flag_scan AS s"
    " WHERE m.mailbox_id = :mailbox_id AND m.remote_ref = s.remote_ref"
    " AND m.flags IS DISTINCT FROM s.flags"
)


@dataclass(slots=True)
class SyncStats:
    folders: int = 0
    # Messages received from the server (new and already stored).
    fetched: int = 0
    # New messages.
    stored: int = 0
    updated: int = 0
    deleted: int = 0
    failed_folders: int = 0


def _utcnow() -> datetime:
    return datetime.now(UTC)


class MailboxSync:
    def __init__(
        self,
        session: AsyncSession,
        mailbox: Mailbox,
        provider: MailProvider,
        storage: AttachmentStorage,
        settings: MailSettings,
        *,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.session = session
        self.mailbox_id = mailbox.id
        self.owner_user_id = mailbox.owner_user_id
        self.provider = provider
        self.storage = storage
        self.sync_settings = SyncSettings.model_validate(mailbox.sync_settings or {})
        self.initial_days = self.sync_settings.initial_sync_days or settings.initial_sync_days
        self.now = now
        self.stats = SyncStats()

    async def run(self) -> SyncStats:
        folders = await self.sync_folders()
        if self.provider.capabilities.mailbox_cursor:
            # Folder-level errors concern the whole mailbox here: they propagate.
            await self.sync_mailbox_scope(frozenset(remote_id for _, remote_id in folders))
            return self.stats
        for folder_id, remote_id in folders:
            try:
                await self.sync_folder(folder_id, remote_id)
            except MAILBOX_ERRORS:
                raise
            except ProviderError as exc:
                await self.session.rollback()
                await self._record_error(folder_id, exc.code)
                self.stats.failed_folders += 1
                log.warning(
                    "mail_folder_sync_failed",
                    mailbox_id=str(self.mailbox_id),
                    folder_id=str(folder_id),
                    error=exc.code,
                )
        return self.stats

    # -- folders ----------------------------------------------------------------------

    def _excluded(self, role: FolderRole | None, remote_id: str) -> bool:
        return role in self.sync_settings.excluded_roles or (
            remote_id in self.sync_settings.excluded_folders
        )

    async def sync_folders(self) -> list[tuple[uuid.UUID, str]]:
        """Mirror the server's folder list; return the folders to sync (INBOX first)."""
        remote: list[RemoteFolder] = await self.provider.list_folders()
        rows = {
            folder.remote_id: folder
            for folder in await self.session.scalars(
                select(Folder).where(Folder.mailbox_id == self.mailbox_id)
            )
        }
        for remote_folder in remote:
            folder = rows.get(remote_folder.remote_id)
            if folder is None:
                folder = Folder(
                    id=uuid7(),
                    mailbox_id=self.mailbox_id,
                    remote_id=remote_folder.remote_id,
                    sync_enabled=not self._excluded(remote_folder.role, remote_folder.remote_id),
                )
                self.session.add(folder)
                rows[remote_folder.remote_id] = folder
            folder.name = remote_folder.name
            folder.kind = remote_folder.kind
            folder.role = remote_folder.role
        await self.session.commit()

        remote_ids = {f.remote_id for f in remote}
        for remote_id in [r for r in rows if r not in remote_ids]:
            folder_id = rows.pop(remote_id).id
            await self._delete_folder_messages(folder_id)
            await self.session.execute(delete(Folder).where(Folder.id == folder_id))
            await self.session.commit()
            log.info(
                "mail_folder_removed", mailbox_id=str(self.mailbox_id), folder_id=str(folder_id)
            )

        selected = [
            f for f in rows.values() if f.sync_enabled and not self._excluded(f.role, f.remote_id)
        ]
        selected.sort(key=lambda f: (f.role is not FolderRole.INBOX, f.remote_id))
        self.stats.folders = len(selected)
        return [(f.id, f.remote_id) for f in selected]

    async def _delete_folder_messages(self, folder_id: uuid.UUID) -> None:
        """Delete the messages of a folder that are in no other folder (commits)."""
        link = message_folders_table
        other = aliased(link)
        refs = await self.session.scalars(
            select(Message.remote_ref)
            .join(link, link.c.message_id == Message.id)
            .where(
                link.c.folder_id == folder_id,
                ~exists().where(other.c.message_id == Message.id, other.c.folder_id != folder_id),
            )
        )
        self.stats.deleted += await delete_messages(
            self.session, self.mailbox_id, list(refs), self.storage
        )

    # -- messages ---------------------------------------------------------------------

    async def sync_folder(self, folder_id: uuid.UUID, remote_id: str) -> None:
        try:
            await self._sync_scope(folder_id, remote_id)
        except CursorInvalidError:
            await self.session.rollback()
            log.warning(
                "mail_sync_cursor_invalid",
                mailbox_id=str(self.mailbox_id),
                folder_id=str(folder_id),
            )
            await self._delete_folder_messages(folder_id)
            await self._reset_cursor(folder_id)
            await self._sync_scope(folder_id, remote_id)

    async def sync_mailbox_scope(self, selected: frozenset[str]) -> None:
        """Sync a provider with a mailbox-wide change log; ``selected`` are the remote IDs
        of the folders whose messages are stored."""
        try:
            await self._sync_scope(None, MAILBOX_SCOPE, selected)
        except CursorInvalidError:
            await self.session.rollback()
            log.warning("mail_sync_cursor_invalid", mailbox_id=str(self.mailbox_id))
            await self._reset_cursor(None)
            await self._sync_scope(None, MAILBOX_SCOPE, selected)

    async def _reset_cursor(self, folder_id: uuid.UUID | None) -> None:
        await self.session.execute(
            update(SyncState)
            .where(SyncState.mailbox_id == self.mailbox_id, SyncState.folder_id == folder_id)
            .values(cursor={})
        )
        await self.session.commit()

    async def _state(self, folder_id: uuid.UUID | None) -> SyncState:
        return await _sync_state(self.session, self.mailbox_id, folder_id)

    async def _has_messages(self) -> bool:
        return bool(
            await self.session.scalar(select(exists().where(Message.mailbox_id == self.mailbox_id)))
        )

    async def _sync_scope(
        self,
        folder_id: uuid.UUID | None,
        remote_id: str,
        selected: frozenset[str] | None = None,
    ) -> None:
        """Apply ``fetch_since(remote_id)``; the cursor is stored in the state of
        ``folder_id`` (``None``: mailbox-wide). With ``selected`` (mailbox-wide change log)
        only messages in one of these folders are kept.

        Mailbox-wide without a cursor but with stored messages means resync (``reconcile``):
        intermediate cursors are not stored, so an interrupted resync starts over, and
        afterwards stored messages of the time window that were not returned are deleted.
        """
        state = await self._state(folder_id)
        folders = {
            f.remote_id: f
            for f in await self.session.scalars(
                select(Folder).where(Folder.mailbox_id == self.mailbox_id)
            )
        }
        cursor = SyncCursor(dict(state.cursor)) if state.cursor else None
        since = None if cursor else self.now() - timedelta(days=self.initial_days)
        reconcile = selected is not None and cursor is None and await self._has_messages()
        seen: set[str] = set()
        final: SyncCursor | None = None

        def wanted(folder_ids: Iterable[str]) -> bool:
            return selected is None or any(f in selected for f in folder_ids)

        new_ids: list[tuple[uuid.UUID, bool]] = []
        updates: dict[str, MessageUpdated] = {}
        deletes: list[str] = []

        async def store(raw: RawMessage, known: uuid.UUID | None, initial: bool) -> None:
            updates.pop(raw.remote_ref, None)
            if not wanted(raw.folder_ids):
                if known is not None:
                    deletes.append(raw.remote_ref)
                return
            seen.add(raw.remote_ref)
            message = await store_message(self.session, self.mailbox_id, raw, self.storage, folders)
            self.stats.fetched += 1
            if known is None:
                new_ids.append((message.id, initial))

        def update(event: MessageUpdated) -> None:
            if event.folder_ids is not None and not wanted(event.folder_ids):
                updates.pop(event.remote_ref, None)
                deletes.append(event.remote_ref)
            else:
                updates[event.remote_ref] = event

        async for event in self.provider.fetch_since(remote_id, cursor, since=since):
            if isinstance(event, MessageFetched):
                raw = event.message
                await store(raw, await self._known(raw.remote_ref), event.initial)
            elif isinstance(event, MessageChanged):
                if await self._known(event.remote_ref) is not None:
                    seen.add(event.remote_ref)
                    update(
                        MessageUpdated(
                            event.remote_ref, flags=event.flags, folder_ids=event.folder_ids
                        )
                    )
                    continue
                if event.folder_ids is not None and not wanted(event.folder_ids):
                    continue
                try:
                    raw = await event.load()
                except MessageNotFoundError:
                    continue
                await store(raw, None, False)
            elif isinstance(event, MessageUpdated):
                update(event)
            elif isinstance(event, MessageDeleted):
                updates.pop(event.remote_ref, None)
                deletes.append(event.remote_ref)
            elif isinstance(event, FlagsReported):
                self.stats.updated += await self._apply_flags(event.flags)
            elif isinstance(event, CursorAdvanced):
                deletes.extend(await self._apply_updates(updates.values(), folders))
                if reconcile:
                    final = event.cursor
                else:
                    state.cursor = event.cursor.data
                state.last_synced_at = self.now()
                state.last_error = None
                if new_ids:
                    await self._publish("progress")
                if deletes:
                    # Commits the transaction, then removes the attachment files.
                    self.stats.deleted += await delete_messages(
                        self.session, self.mailbox_id, deletes, self.storage
                    )
                else:
                    await self.session.commit()
                self.stats.stored += len(new_ids)
                for message_id, initial in new_ids:
                    await message_stored(self.mailbox_id, message_id, backfill=initial)
                new_ids, deletes = [], []
                updates.clear()
                self._release_messages()
        if reconcile and final is not None and since is not None:
            await self._delete_unseen(seen, since)
            state = await self._state(folder_id)
            state.cursor = final.data
            await self.session.commit()

    async def _delete_unseen(self, seen: set[str], since: datetime) -> None:
        """Resync: delete stored messages received since ``since`` that the server did not
        return (commits)."""
        refs = await self.session.scalars(
            select(Message.remote_ref).where(
                Message.mailbox_id == self.mailbox_id, Message.received_at >= since
            )
        )
        gone = [ref for ref in refs if ref not in seen]
        if gone:
            self.stats.deleted += await delete_messages(
                self.session, self.mailbox_id, gone, self.storage
            )

    async def _known(self, remote_ref: str) -> uuid.UUID | None:
        return await self.session.scalar(
            select(Message.id).where(
                Message.mailbox_id == self.mailbox_id, Message.remote_ref == remote_ref
            )
        )

    async def _apply_updates(
        self, events: Iterable[MessageUpdated], folders: dict[str, Folder]
    ) -> list[str]:
        """Apply flag/folder changes; return the references of messages that are in no
        synced folder any more (to be deleted)."""
        unsynced: list[str] = []
        synced = {
            remote_id
            for remote_id, folder in folders.items()
            if folder.sync_enabled and not self._excluded(folder.role, remote_id)
        }
        by_ref = {event.remote_ref: event for event in events}
        refs = list(by_ref)
        for start in range(0, len(refs), _UPDATE_CHUNK):
            rows = await self.session.execute(
                select(Message.id, Message.remote_ref, Message.flags).where(
                    Message.mailbox_id == self.mailbox_id,
                    Message.remote_ref.in_(refs[start : start + _UPDATE_CHUNK]),
                )
            )
            for message_id, remote_ref, flags in rows.all():
                event = by_ref[remote_ref]
                changed = False
                if event.flags is not None and sorted(event.flags) != list(flags):
                    await self.session.execute(
                        update(Message)
                        .where(Message.id == message_id)
                        .values(flags=sorted(event.flags))
                    )
                    changed = True
                if event.folder_ids is not None and not synced.intersection(event.folder_ids):
                    unsynced.append(remote_ref)
                    continue
                if event.folder_ids is not None:
                    message = await self.session.get(
                        Message, message_id, options=[selectinload(Message.folders)]
                    )
                    if message is not None:
                        message.folders = [folders[f] for f in event.folder_ids if f in folders]
                        changed = True
                self.stats.updated += changed
        return unsynced

    async def _apply_flags(self, flags: Mapping[str, frozenset[str]]) -> int:
        """``FlagsReported``: load the reported flags into a temporary table and update the
        stored messages whose flags differ, in one statement. Returns how many changed.
        Committed with the next ``CursorAdvanced``."""
        if not flags:
            return 0
        await self.session.execute(text(_FLAG_SCAN_TABLE))
        await self.session.execute(text("TRUNCATE mail_flag_scan"))
        payload = json.dumps({ref: sorted(value) for ref, value in flags.items()})
        await self.session.execute(text(_FLAG_SCAN_LOAD), {"flags": payload})
        await self.session.execute(text("ANALYZE mail_flag_scan"))
        result = await self.session.execute(text(_FLAG_SCAN_APPLY), {"mailbox_id": self.mailbox_id})
        return int(result.rowcount)  # type: ignore[attr-defined]

    def _release_messages(self) -> None:
        """Keep the session small during long imports: drop committed messages."""
        for instance in list(self.session.identity_map.values()):
            # Attachments follow their message (cascade).
            if isinstance(instance, Message) and instance in self.session:
                self.session.expunge(instance)

    # -- status -----------------------------------------------------------------------

    async def _record_error(self, folder_id: uuid.UUID | None, code: str) -> None:
        state = await self._state(folder_id)
        state.last_error = code[:64]
        await self.session.commit()

    async def _publish(self, status: str) -> None:
        await _publish(self.session, self.mailbox_id, self.owner_user_id, status)


async def _sync_state(
    session: AsyncSession, mailbox_id: uuid.UUID, folder_id: uuid.UUID | None
) -> SyncState:
    state = await session.scalar(
        select(SyncState).where(
            SyncState.mailbox_id == mailbox_id, SyncState.folder_id == folder_id
        )
    )
    if state is None:
        state = SyncState(id=uuid7(), mailbox_id=mailbox_id, folder_id=folder_id, cursor={})
        session.add(state)
        await session.flush()
    return state


async def _publish(
    session: AsyncSession, mailbox_id: uuid.UUID, owner_user_id: uuid.UUID | None, status: str
) -> None:
    event = Event(type="mailbox.sync", ids={"mailbox_id": mailbox_id}, status=status)
    if owner_user_id is not None:
        await publish(session, owner_user_id, event)
    else:
        await publish_to_readers(session, mailbox_id, event)


async def _finish(
    session: AsyncSession,
    mailbox_id: uuid.UUID,
    owner_user_id: uuid.UUID | None,
    error: ProviderError | None,
    now: datetime,
) -> None:
    """Record the mailbox-level result and notify the owner (commits)."""
    state = await _sync_state(session, mailbox_id, None)
    if error is None:
        state.last_synced_at = now
        state.last_error = None
    else:
        state.last_error = error.code[:64]
    await _publish(session, mailbox_id, owner_user_id, "failed" if error else "done")
    await session.commit()


def credentials_saver(
    sessionmaker: async_sessionmaker[AsyncSession], mailbox_id: uuid.UUID
) -> SaveCredentials:
    """``MailboxConfig.save_credentials`` for a mailbox: stores the (encrypted) credentials
    in a transaction of its own, so they survive a sync that fails afterwards."""

    async def save(credentials: dict[str, Any]) -> None:
        async with sessionmaker() as session:
            await session.execute(
                update(Mailbox).where(Mailbox.id == mailbox_id).values(credentials=credentials)
            )
            await session.commit()
        log.info("mail_credentials_saved", mailbox_id=str(mailbox_id))

    return save


def mailbox_config(
    mailbox: Mailbox, *, save_credentials: SaveCredentials | None = None
) -> MailboxConfig:
    return MailboxConfig(
        mailbox_id=mailbox.id,
        type=mailbox.type,
        address=mailbox.address,
        settings=dict(mailbox.provider_settings or {}),
        credentials=dict(mailbox.credentials or {}),
        save_credentials=save_credentials,
    )


async def sync_mailbox(
    session: AsyncSession,
    mailbox_id: uuid.UUID,
    *,
    storage: AttachmentStorage,
    settings: MailSettings,
    provider_factory: ProviderFactory = registry.create,
    now: Callable[[], datetime] = _utcnow,
) -> SyncStats | None:
    """Sync all selected folders of a mailbox. Returns ``None`` if the mailbox does not
    exist, is disabled or failed. Raises ``ConnectionFailedError`` (after recording it) so
    the job is retried; other provider errors are only recorded."""
    mailbox = await session.get(Mailbox, mailbox_id)
    if mailbox is None or not mailbox.sync_enabled:
        log.info("mail_sync_skipped", mailbox_id=str(mailbox_id))
        return None
    owner_user_id = mailbox.owner_user_id
    # Credentials are saved outside the sync's transaction (same connection pool).
    saver = credentials_saver(
        async_sessionmaker(
            session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint"
        ),
        mailbox_id,
    )
    try:
        provider = provider_factory(mailbox_config(mailbox, save_credentials=saver))
        try:
            stats = await MailboxSync(session, mailbox, provider, storage, settings, now=now).run()
        finally:
            await provider.aclose()
    except ProviderError as exc:
        await session.rollback()
        await _finish(session, mailbox_id, owner_user_id, exc, now())
        log.warning("mail_sync_failed", mailbox_id=str(mailbox_id), error=exc.code)
        if isinstance(exc, ConnectionFailedError):
            raise
        return None
    await _finish(session, mailbox_id, owner_user_id, None, now())
    log.info("mail_sync_finished", mailbox_id=str(mailbox_id), **asdict(stats))
    return stats
