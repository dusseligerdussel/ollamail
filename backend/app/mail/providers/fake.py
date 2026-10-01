"""In-memory ``MailProvider`` for tests of other modules (sync, triage, todos, ...).

    provider = FakeMailProvider()
    provider.add_folder(RemoteFolder("INBOX", "Inbox", role=FolderRole.INBOX))
    ref = provider.add_message("INBOX", raw_bytes)
    registry.register(MailboxType.IMAP, lambda config: provider, replace=True)

It behaves like a server with a change log: ``fetch_since`` without a cursor returns the
folder's current content, with a cursor only the changes after it. ``labels=True`` makes
it behave like Gmail (a message can be in several folders, ``move`` keeps the reference),
otherwise like IMAP (``move`` assigns a new reference). Every server-side action is
recorded in ``actions`` for assertions.
"""

import asyncio
import itertools
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Literal

from app.mail.models import FolderKind
from app.mail.providers.base import (
    ChangeEvent,
    CursorAdvanced,
    CursorInvalidError,
    MailboxConfig,
    MessageDeleted,
    MessageFetched,
    MessageNotFoundError,
    MessageUpdated,
    ProviderCapabilities,
    RawMessage,
    RemoteFolder,
    SyncCursor,
    SyncEvent,
)

ChangeKind = Literal["added", "deleted", "updated"]


@dataclass(slots=True)
class _Change:
    seq: int
    kind: ChangeKind
    remote_ref: str
    folder_ids: tuple[str, ...]


@dataclass(slots=True)
class _Action:
    name: str
    args: tuple[Any, ...]


@dataclass(slots=True)
class _State:
    folders: dict[str, RemoteFolder] = field(default_factory=dict)
    messages: dict[str, RawMessage] = field(default_factory=dict)
    log: list[_Change] = field(default_factory=list)


class FakeMailProvider:
    def __init__(
        self,
        config: MailboxConfig | None = None,
        *,
        labels: bool = False,
        push: bool = True,
    ) -> None:
        self.config = config
        self.capabilities = ProviderCapabilities(
            labels=labels, push=push, server_threads=labels, keywords=True
        )
        self.actions: list[_Action] = []
        self.closed = False
        self._state = _State()
        self._seq = itertools.count(1)
        self._ref_ids = itertools.count(1)
        self._epoch = 0
        self._watchers: list[tuple[str | None, asyncio.Queue[ChangeEvent]]] = []

    # -- test setup ---------------------------------------------------------------------

    def add_folder(self, folder: RemoteFolder) -> None:
        if self.capabilities.labels and folder.kind is FolderKind.FOLDER:
            folder = replace(folder, kind=FolderKind.LABEL)
        self._state.folders[folder.remote_id] = folder

    def add_message(
        self,
        folder_ids: str | tuple[str, ...],
        raw: bytes,
        *,
        flags: frozenset[str] = frozenset(),
        provider_thread_id: str | None = None,
        received_at: datetime | None = None,
    ) -> str:
        """Simulate a message arriving on the server; returns its ``remote_ref``."""
        folders = (folder_ids,) if isinstance(folder_ids, str) else folder_ids
        for folder_id in folders:
            self._require_folder(folder_id)
        ref = f"fake-{next(self._ref_ids)}"
        self._state.messages[ref] = RawMessage(
            remote_ref=ref,
            raw=raw,
            folder_ids=folders,
            flags=flags,
            provider_thread_id=provider_thread_id,
            received_at=received_at,
        )
        self._record("added", ref, folders)
        return ref

    def delete_message(self, remote_ref: str) -> None:
        """Simulate a message being deleted on the server."""
        message = self._get(remote_ref)
        del self._state.messages[remote_ref]
        self._record("deleted", remote_ref, message.folder_ids)

    def invalidate_cursors(self) -> None:
        """Simulate UIDVALIDITY change / expired delta token."""
        self._epoch += 1

    @property
    def messages(self) -> dict[str, RawMessage]:
        return dict(self._state.messages)

    # -- MailProvider ---------------------------------------------------------------------

    async def list_folders(self) -> list[RemoteFolder]:
        self.actions.append(_Action("list_folders", ()))
        return list(self._state.folders.values())

    async def fetch_since(
        self, folder_id: str, cursor: SyncCursor | None, *, since: datetime | None = None
    ) -> AsyncIterator[SyncEvent]:
        self._require_folder(folder_id)
        current = self._state.log[-1].seq if self._state.log else 0
        if cursor is None:
            for message in list(self._state.messages.values()):
                if folder_id not in message.folder_ids:
                    continue
                if since and message.received_at and message.received_at < since:
                    continue
                yield MessageFetched(message)
        else:
            if cursor.data.get("epoch") != self._epoch:
                raise CursorInvalidError()
            after = int(cursor.data.get("seq", 0))
            for change in [c for c in self._state.log if c.seq > after]:
                event = self._event_for(change, folder_id)
                if event is not None:
                    yield event
        yield CursorAdvanced(SyncCursor({"epoch": self._epoch, "seq": current}))

    async def watch(self, folder_id: str | None = None) -> AsyncIterator[ChangeEvent]:
        if not self.capabilities.push:
            raise NotImplementedError("push is disabled for this fake provider")
        queue: asyncio.Queue[ChangeEvent] = asyncio.Queue()
        entry = (folder_id, queue)
        self._watchers.append(entry)
        try:
            while True:
                yield await queue.get()
        finally:
            self._watchers.remove(entry)

    async def move(self, remote_ref: str, target_folder_id: str) -> str:
        self.actions.append(_Action("move", (remote_ref, target_folder_id)))
        self._require_folder(target_folder_id)
        message = self._get(remote_ref)
        if self.capabilities.labels:
            folders = (*[f for f in message.folder_ids if f != target_folder_id], target_folder_id)
            self._update(replace(message, folder_ids=folders))
            return remote_ref
        self.delete_message(remote_ref)
        return self.add_message(
            target_folder_id,
            message.raw,
            flags=message.flags,
            provider_thread_id=message.provider_thread_id,
            received_at=message.received_at,
        )

    async def set_flags(self, remote_ref: str, flags: frozenset[str]) -> None:
        self.actions.append(_Action("set_flags", (remote_ref, flags)))
        self._update(replace(self._get(remote_ref), flags=frozenset(flags)))

    async def apply_label(self, remote_ref: str, label: str) -> None:
        self.actions.append(_Action("apply_label", (remote_ref, label)))
        message = self._get(remote_ref)
        if self.capabilities.labels:
            self._require_folder(label)
            if label not in message.folder_ids:
                self._update(replace(message, folder_ids=(*message.folder_ids, label)))
        else:
            self._update(replace(message, flags=message.flags | {label}))

    async def remove_label(self, remote_ref: str, label: str) -> None:
        self.actions.append(_Action("remove_label", (remote_ref, label)))
        message = self._get(remote_ref)
        if self.capabilities.labels:
            folders = tuple(f for f in message.folder_ids if f != label)
            self._update(replace(message, folder_ids=folders))
        else:
            self._update(replace(message, flags=message.flags - {label}))

    async def aclose(self) -> None:
        self.closed = True

    # -- internals ----------------------------------------------------------------------

    def _require_folder(self, folder_id: str) -> None:
        if folder_id not in self._state.folders:
            raise KeyError(f"unknown folder {folder_id!r}")

    def _get(self, remote_ref: str) -> RawMessage:
        try:
            return self._state.messages[remote_ref]
        except KeyError:
            raise MessageNotFoundError() from None

    def _update(self, message: RawMessage) -> None:
        previous = self._state.messages[message.remote_ref]
        self._state.messages[message.remote_ref] = message
        self._record(
            "updated",
            message.remote_ref,
            tuple(dict.fromkeys((*previous.folder_ids, *message.folder_ids))),
        )

    def _record(self, kind: ChangeKind, remote_ref: str, folder_ids: tuple[str, ...]) -> None:
        self._state.log.append(_Change(next(self._seq), kind, remote_ref, folder_ids))
        for watched, queue in self._watchers:
            for folder_id in folder_ids:
                if watched is None or watched == folder_id:
                    queue.put_nowait(ChangeEvent(folder_id))

    def _event_for(self, change: _Change, folder_id: str) -> SyncEvent | None:
        if folder_id not in change.folder_ids:
            return None
        message = self._state.messages.get(change.remote_ref)
        if change.kind == "deleted" or message is None:
            return MessageDeleted(change.remote_ref)
        if folder_id not in message.folder_ids:
            # Label removed or moved away: gone from this folder's point of view.
            return MessageDeleted(change.remote_ref)
        if change.kind == "added":
            return MessageFetched(message)
        return MessageUpdated(change.remote_ref, flags=message.flags, folder_ids=message.folder_ids)
