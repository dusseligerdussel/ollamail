"""Gmail / Google Workspace ``MailProvider`` via the Gmail REST API
(docs/providers/gmail.md, docs/ARCHITECTURE.md §3.1).

Settings (``Mailbox.provider_settings``, validated by ``GmailProviderSettings``):
``auth`` (``oauth``: refresh token in ``Mailbox.credentials``; ``service_account``:
Workspace domain-wide delegation with the instance's key file), ``include_spam_trash`` and
the optional Pub/Sub pull subscription (``pubsub_topic``, ``pubsub_subscription``).

Labels are folders (``FolderKind.LABEL``): the system labels INBOX, SENT, DRAFT, SPAM and
TRASH, all user labels and the virtual ``ALL_MAIL`` ("All Mail": every message outside spam
and trash). ``UNREAD``/``STARRED``/``DRAFT`` become flags. ``remote_ref`` is the Gmail
message ID, which never changes.

Gmail keeps one change log per mailbox (``historyId``), so the provider declares
``mailbox_cursor`` and syncs ``MAILBOX_SCOPE``. Cursor ``{"v": 1, "history_id",
"import": {"q", "page_token"}}``:

* without a cursor: remember the current ``historyId`` (``users/me/profile``), then import
  ``messages.list`` (``after:<since>``, newest first) page by page; every page ends with
  ``CursorAdvanced``, so an interrupted import resumes;
* with a cursor: ``history.list`` from ``history_id`` (a 404 means the history expired:
  ``CursorInvalidError``, the engine resyncs), then a pending import continues.

Sending (``send``): ``messages.send`` with the RFC 5322 source and the ``threadId`` of the
answered mail (its ``In-Reply-To``/``References``/subject keep Gmail's threading); Gmail
files the copy under SENT itself. ``gmail.modify`` covers sending; with
``OLLAMAIL_GMAIL_READONLY`` sending is refused (``read_only``).

Message sources (``format=raw``) are fetched with batch requests. Push: ``watch`` uses a
Pub/Sub *pull* subscription (no public URL needed); without one the provider has no push
and the watcher polls.
"""

import asyncio
import base64
import binascii
import json
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import GmailSettings, MailSettings, get_settings
from app.core.logging import get_logger
from app.mail.models import FolderKind, FolderRole, MailboxType
from app.mail.providers.base import (
    MAILBOX_SCOPE,
    AuthenticationError,
    ChangeEvent,
    ConfigurationError,
    CursorAdvanced,
    CursorInvalidError,
    Flag,
    MailboxConfig,
    MessageDeleted,
    MessageFetched,
    MessageNotFoundError,
    MessageUpdated,
    OutgoingReply,
    ProviderCapabilities,
    ProviderError,
    RawMessage,
    RemoteFolder,
    SendError,
    SentMessage,
    SyncCursor,
    SyncEvent,
)
from app.mail.providers.gmail_api import (
    GOOGLE_API,
    PUBSUB_API,
    BadRequestError,
    ConflictError,
    GoogleApiClient,
    NotFoundError,
    Sleep,
    raise_for_status,
)
from app.mail.providers.gmail_auth import (
    SCOPE_PUBSUB,
    RefreshTokenSource,
    ServiceAccountTokenSource,
    TokenSource,
    gmail_scope,
    load_service_account_key,
)
from app.mail.providers.registry import registry

log = get_logger(__name__)

CURSOR_VERSION = 1
ALL_MAIL = "ALL_MAIL"
INBOX, SPAM, TRASH, UNREAD, STARRED, DRAFT, CHAT = (
    "INBOX",
    "SPAM",
    "TRASH",
    "UNREAD",
    "STARRED",
    "DRAFT",
    "CHAT",
)
USER_PATH = "/gmail/v1/users/me"
# Message sources per batch request: bounds memory (a raw message can be ~35 MB in JSON).
RAW_BATCH = 20
HISTORY_PAGE = 500
HISTORY_TYPES = ("messageAdded", "messageDeleted", "labelAdded", "labelRemoved")
# Google requires renewing ``users.watch`` at least every 7 days.
WATCH_RENEW_SECONDS = 24 * 3600
PULL_TIMEOUT = 90.0
PULL_MAX_MESSAGES = 10

_SYSTEM_FOLDERS: dict[str, tuple[str, FolderRole]] = {
    INBOX: ("Inbox", FolderRole.INBOX),
    "SENT": ("Sent", FolderRole.SENT),
    DRAFT: ("Drafts", FolderRole.DRAFTS),
    SPAM: ("Spam", FolderRole.JUNK),
    TRASH: ("Trash", FolderRole.TRASH),
}
# System labels that are flags or categories, not folders.
_NOT_FOLDERS = frozenset({UNREAD, STARRED, "IMPORTANT", CHAT})
_HIDDEN = frozenset({SPAM, TRASH})


class GmailProviderSettings(BaseModel):
    """``Mailbox.provider_settings`` of a Gmail mailbox."""

    model_config = ConfigDict(extra="ignore")

    auth: Literal["oauth", "service_account"] = "oauth"
    # Also sync messages that are only in spam or trash (subject to the folder exclusions
    # of the sync settings, which skip both by default).
    include_spam_trash: bool = False
    # Pub/Sub pull subscription for push notifications (optional, needs GCP).
    pubsub_topic: str | None = Field(default=None, pattern=r"^projects/[^/]+/topics/[^/]+$")
    pubsub_subscription: str | None = Field(
        default=None, pattern=r"^projects/[^/]+/subscriptions/[^/]+$"
    )


@dataclass(slots=True)
class _Cursor:
    history_id: str
    importing: bool = False
    import_q: str = ""
    page_token: str | None = None

    @classmethod
    def load(cls, cursor: SyncCursor) -> "_Cursor":
        data = cursor.data
        try:
            if data.get("v") != CURSOR_VERSION:
                raise ValueError
            history_id = str(int(data["history_id"]))
            pending = data.get("import")
            if pending is None:
                return cls(history_id)
            token = pending.get("page_token")
            return cls(
                history_id,
                importing=True,
                import_q=str(pending.get("q") or ""),
                page_token=str(token) if token else None,
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            raise CursorInvalidError() from None

    def dump(self) -> SyncCursor:
        data: dict[str, Any] = {"v": CURSOR_VERSION, "history_id": self.history_id}
        if self.importing:
            data["import"] = {"q": self.import_q, "page_token": self.page_token}
        return SyncCursor(data)


@dataclass(slots=True)
class _Change:
    kind: Literal["added", "deleted", "labels"]
    label_ids: tuple[str, ...] = ()


def _decode_raw(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class GmailProvider:
    def __init__(
        self,
        config: MailboxConfig,
        *,
        settings: GmailSettings | None = None,
        mail_settings: MailSettings | None = None,
        http: httpx.AsyncClient | None = None,
        token_source: TokenSource | None = None,
        pubsub_token_source: TokenSource | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        """``token_source``/``pubsub_token_source`` replace the configured tokens (tests)."""
        instance = get_settings()
        self.gmail_settings = settings or instance.gmail
        try:
            self.settings = GmailProviderSettings.model_validate(config.settings)
        except ValidationError:
            raise ConfigurationError() from None
        self.config = config
        self._batch_size = (mail_settings or instance.mail).sync_batch_size
        self._sleep = sleep
        push = bool(
            self.settings.pubsub_topic
            and self.settings.pubsub_subscription
            and (pubsub_token_source is not None or self.gmail_settings.service_account_file)
        )
        self.capabilities = ProviderCapabilities(
            labels=True, push=push, server_threads=True, keywords=False, mailbox_cursor=True
        )
        # Validate before creating the HTTP client, so a misconfigured mailbox leaks nothing.
        prepare = None if token_source is not None else self._token_factory()
        self._own_http = http is None
        self._http = http or httpx.AsyncClient(timeout=self.gmail_settings.timeout)
        if token_source is None:
            assert prepare is not None
            token_source = prepare(self._http)
        self._pubsub_tokens = pubsub_token_source
        self._api = GoogleApiClient(
            self._http, token_source, f"{GOOGLE_API}{USER_PATH}", sleep=sleep
        )
        self._labels: dict[str, dict[str, Any]] | None = None

    def _token_factory(self) -> Callable[[httpx.AsyncClient], TokenSource]:
        settings = self.gmail_settings
        if self.settings.auth == "service_account":
            if settings.service_account_file is None:
                raise ConfigurationError(code="service_account_missing")
            key = load_service_account_key(settings.service_account_file)
            scope, subject = gmail_scope(settings), self.config.address
            return lambda http: ServiceAccountTokenSource(http, key, scope, subject=subject)
        refresh_token = self.config.credentials.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise AuthenticationError(code="credentials_missing")
        if not settings.client_id or settings.client_secret is None:
            raise ConfigurationError(code="oauth_not_configured")
        return lambda http: RefreshTokenSource(http, settings, refresh_token)

    # -- labels -----------------------------------------------------------------------

    async def _label_map(self, *, reload: bool = False) -> dict[str, dict[str, Any]]:
        if self._labels is None or reload:
            data = await self._api.request("GET", "/labels")
            self._labels = {
                str(label["id"]): label
                for label in data.get("labels") or ()
                if isinstance(label, dict) and label.get("id")
            }
        return self._labels

    async def list_folders(self) -> list[RemoteFolder]:
        labels = await self._label_map(reload=True)
        system = [
            RemoteFolder(label_id, name, FolderKind.LABEL, role)
            for label_id, (name, role) in _SYSTEM_FOLDERS.items()
            if label_id in labels
        ]
        by_name = {
            str(label.get("name")): label_id
            for label_id, label in labels.items()
            if label.get("type") == "user"
        }
        user = [
            RemoteFolder(
                label_id,
                name,
                FolderKind.LABEL,
                parent_id=by_name.get(name.rpartition("/")[0]),
            )
            for name, label_id in sorted(by_name.items())
        ]
        return [
            *system,
            RemoteFolder(ALL_MAIL, "All Mail", FolderKind.LABEL, FolderRole.ALL),
            *user,
        ]

    @staticmethod
    def _folder_ids(label_ids: Iterable[str]) -> tuple[str, ...]:
        labels = tuple(label_ids)
        folders = [
            label
            for label in labels
            if label not in _NOT_FOLDERS and not label.startswith("CATEGORY_")
        ]
        if not _HIDDEN.intersection(labels):
            folders.append(ALL_MAIL)
        return tuple(folders)

    @staticmethod
    def _flags(label_ids: Iterable[str]) -> frozenset[str]:
        labels = set(label_ids)
        flags = set()
        if UNREAD not in labels:
            flags.add(Flag.SEEN.value)
        if STARRED in labels:
            flags.add(Flag.FLAGGED.value)
        if DRAFT in labels:
            flags.add(Flag.DRAFT.value)
        return frozenset(flags)

    def _hidden(self, label_ids: Iterable[str]) -> bool:
        return not self.settings.include_spam_trash and bool(_HIDDEN.intersection(label_ids))

    def _message(self, data: dict[str, Any]) -> RawMessage:
        try:
            label_ids = _labels(data)
            internal = data.get("internalDate")
            received = (
                datetime.fromtimestamp(int(internal) / 1000, UTC) if internal is not None else None
            )
            return RawMessage(
                remote_ref=str(data["id"]),
                raw=_decode_raw(str(data["raw"])),
                folder_ids=self._folder_ids(label_ids),
                flags=self._flags(label_ids),
                provider_thread_id=str(data["threadId"]) if data.get("threadId") else None,
                received_at=received,
            )
        except (KeyError, TypeError, ValueError, binascii.Error):
            raise ProviderError(code="invalid_response") from None

    async def _fetch_raw(
        self, ids: list[str]
    ) -> AsyncIterator[tuple[str, RawMessage | None, tuple[str, ...]]]:
        """Sources and label IDs of ``ids`` (``None``: the message is gone)."""
        for start in range(0, len(ids), RAW_BATCH):
            chunk = ids[start : start + RAW_BATCH]
            responses = await self._api.batch(
                [f"{USER_PATH}/messages/{message_id}?format=raw" for message_id in chunk]
            )
            for message_id, (status, data) in zip(chunk, responses, strict=True):
                if status == 404:
                    yield message_id, None, ()
                    continue
                raise_for_status(status, data)
                if not isinstance(data, dict):
                    raise ProviderError(code="invalid_response")
                yield message_id, self._message(data), _labels(data)

    # -- sync -------------------------------------------------------------------------

    async def fetch_since(
        self, folder_id: str, cursor: SyncCursor | None, *, since: datetime | None = None
    ) -> AsyncIterator[SyncEvent]:
        if folder_id != MAILBOX_SCOPE:
            # Gmail has one change log per mailbox (capabilities.mailbox_cursor).
            raise ProviderError(code="folder_scope_unsupported")
        if cursor is None:
            profile = await self._api.request("GET", "/profile")
            history_id = profile.get("historyId")
            if history_id is None:
                raise ProviderError(code="invalid_response")
            query = f"after:{int(since.timestamp())}" if since else ""
            state = _Cursor(str(history_id), importing=True, import_q=query)
        else:
            state = _Cursor.load(cursor)
            async for event in self._history(state):
                yield event
        if state.importing:
            async for event in self._import(state):
                yield event
        yield CursorAdvanced(state.dump())

    async def _import(self, state: _Cursor) -> AsyncIterator[SyncEvent]:
        restarted = False
        while True:
            params: dict[str, Any] = {
                "maxResults": self._batch_size,
                "includeSpamTrash": str(self.settings.include_spam_trash).lower(),
            }
            if state.import_q:
                params["q"] = state.import_q
            if state.page_token:
                params["pageToken"] = state.page_token
            try:
                page = await self._api.request("GET", "/messages", params=params)
            except BadRequestError:
                if not state.page_token or restarted:
                    raise
                # Expired page token: list again; stored messages are only updated.
                state.page_token, restarted = None, True
                continue
            ids = [str(m["id"]) for m in page.get("messages") or () if isinstance(m, dict)]
            async for _, message, labels in self._fetch_raw(ids):
                if message is None or CHAT in labels or self._hidden(labels):
                    continue
                yield MessageFetched(message, initial=True)
            token = page.get("nextPageToken")
            if not token:
                state.importing, state.page_token = False, None
                return
            state.page_token = str(token)
            yield CursorAdvanced(state.dump())

    async def _history(self, state: _Cursor) -> AsyncIterator[SyncEvent]:
        # The start ID stays the same while paging through one history listing.
        start, page_token = state.history_id, None
        while True:
            params: dict[str, Any] = {
                "startHistoryId": start,
                "maxResults": HISTORY_PAGE,
                "historyTypes": list(HISTORY_TYPES),
            }
            if page_token:
                params["pageToken"] = page_token
            try:
                page = await self._api.request("GET", "/history", params=params)
            except (NotFoundError, BadRequestError):
                # The start ID is too old (404) or invalid (400): full resync.
                raise CursorInvalidError() from None
            records = [r for r in page.get("history") or () if isinstance(r, dict)]
            async for event in self._apply_history(records):
                yield event
            page_token = page.get("nextPageToken")
            if not page_token:
                if page.get("historyId") is not None:
                    state.history_id = str(page["historyId"])
                return
            if records and records[-1].get("id") is not None:
                state.history_id = str(records[-1]["id"])
                yield CursorAdvanced(state.dump())

    async def _apply_history(self, records: list[dict[str, Any]]) -> AsyncIterator[SyncEvent]:
        changes: dict[str, _Change] = {}
        for record in records:
            for item in record.get("messagesAdded") or ():
                message = item.get("message") or {}
                if message.get("id"):
                    changes[str(message["id"])] = _Change("added", _labels(message))
            for item in record.get("messagesDeleted") or ():
                message = item.get("message") or {}
                if message.get("id"):
                    changes[str(message["id"])] = _Change("deleted")
            for key in ("labelsAdded", "labelsRemoved"):
                for item in record.get(key) or ():
                    message = item.get("message") or {}
                    if not message.get("id"):
                        continue
                    message_id = str(message["id"])
                    previous = changes.get(message_id)
                    if previous is not None and previous.kind == "deleted":
                        continue
                    # Back from spam/trash: the message is unknown locally, fetch it.
                    restored = key == "labelsRemoved" and bool(
                        not self.settings.include_spam_trash
                        and _HIDDEN.intersection(item.get("labelIds") or ())
                    )
                    added = restored or (previous is not None and previous.kind == "added")
                    changes[message_id] = _Change("added" if added else "labels", _labels(message))

        fetch: list[str] = []
        for message_id, change in changes.items():
            if change.kind == "deleted":
                yield MessageDeleted(message_id)
            elif CHAT in change.label_ids:
                continue
            elif self._hidden(change.label_ids):
                yield MessageDeleted(message_id)
            elif change.kind == "labels":
                yield MessageUpdated(
                    message_id,
                    flags=self._flags(change.label_ids),
                    folder_ids=self._folder_ids(change.label_ids),
                )
            else:
                fetch.append(message_id)
        async for message_id, message, labels in self._fetch_raw(fetch):
            if message is None or self._hidden(labels):
                yield MessageDeleted(message_id)
            elif CHAT not in labels:
                yield MessageFetched(message)

    # -- push -------------------------------------------------------------------------

    async def watch(self, folder_id: str | None = None) -> AsyncIterator[ChangeEvent]:
        if not self.capabilities.push:
            raise NotImplementedError("Gmail push needs a Pub/Sub subscription")
        topic, subscription = self.settings.pubsub_topic, self.settings.pubsub_subscription
        assert topic and subscription
        pubsub = GoogleApiClient(
            self._http, self._pubsub_token_source(), PUBSUB_API, sleep=self._sleep
        )
        loop = asyncio.get_running_loop()
        renew_at = 0.0
        address = self.config.address.casefold()
        while True:
            if loop.time() >= renew_at:
                await self._api.request("POST", "/watch", json={"topicName": topic})
                renew_at = loop.time() + WATCH_RENEW_SECONDS
            data = await pubsub.request(
                "POST",
                f"/{subscription}:pull",
                json={"maxMessages": PULL_MAX_MESSAGES},
                request_timeout=max(PULL_TIMEOUT, self.gmail_settings.timeout),
            )
            own: list[str] = []
            other: list[str] = []
            for item in data.get("receivedMessages") or ():
                ack_id = item.get("ackId")
                if not isinstance(ack_id, str):
                    continue
                target = own if _notification_address(item) == address else other
                target.append(ack_id)
            if own:
                await pubsub.request("POST", f"/{subscription}:acknowledge", json={"ackIds": own})
            if other:
                # Not ours (shared subscription): hand back immediately.
                await pubsub.request(
                    "POST",
                    f"/{subscription}:modifyAckDeadline",
                    json={"ackIds": other, "ackDeadlineSeconds": 0},
                )
            if own:
                yield ChangeEvent(None)
            elif not other:
                await self._sleep(1.0)

    def _pubsub_token_source(self) -> TokenSource:
        if self._pubsub_tokens is None:
            path = self.gmail_settings.service_account_file
            if path is None:
                raise ConfigurationError(code="service_account_missing")
            self._pubsub_tokens = ServiceAccountTokenSource(
                self._http, load_service_account_key(path), SCOPE_PUBSUB
            )
        return self._pubsub_tokens

    # -- actions ----------------------------------------------------------------------

    def _require_write(self) -> None:
        if self.gmail_settings.readonly:
            raise ProviderError(code="read_only")

    async def _modify(
        self, remote_ref: str, add: Iterable[str] = (), remove: Iterable[str] = ()
    ) -> None:
        self._require_write()
        try:
            await self._api.request(
                "POST",
                f"/messages/{remote_ref}/modify",
                json={"addLabelIds": list(add), "removeLabelIds": list(remove)},
            )
        except NotFoundError:
            raise MessageNotFoundError() from None

    async def _label_id(self, label: str, *, create: bool = False) -> str | None:
        if label == ALL_MAIL:
            raise ProviderError(code="invalid_label")
        labels = await self._label_map()
        if label in labels:
            return label
        wanted = label.casefold()
        for label_id, data in labels.items():
            if str(data.get("name", "")).casefold() == wanted:
                return label_id
        if not create:
            return None
        self._require_write()
        try:
            created = await self._api.request(
                "POST",
                "/labels",
                json={
                    "name": label,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                },
            )
        except ConflictError:
            # Created concurrently: look it up again.
            await self._label_map(reload=True)
            return await self._label_id(label)
        except BadRequestError:
            raise ProviderError(code="invalid_label") from None
        labels[str(created["id"])] = created
        return str(created["id"])

    async def set_flags(self, remote_ref: str, flags: frozenset[str]) -> None:
        add: list[str] = []
        remove: list[str] = []
        (remove if Flag.SEEN in flags else add).append(UNREAD)
        (add if Flag.FLAGGED in flags else remove).append(STARRED)
        await self._modify(remote_ref, add, remove)

    async def move(self, remote_ref: str, target_folder_id: str) -> str:
        """``TRASH``: move to trash; ``ALL_MAIL``: archive (remove INBOX); otherwise add
        the target label and leave inbox, spam and trash. The reference stays the same."""
        self._require_write()
        if target_folder_id == TRASH:
            try:
                await self._api.request("POST", f"/messages/{remote_ref}/trash")
            except NotFoundError:
                raise MessageNotFoundError() from None
            return remote_ref
        if target_folder_id == ALL_MAIL:
            await self._modify(remote_ref, remove=[INBOX, SPAM, TRASH])
            return remote_ref
        label_id = await self._label_id(target_folder_id)
        if label_id is None:
            raise ProviderError(code="folder_not_found")
        remove = [label for label in (INBOX, SPAM, TRASH) if label != label_id]
        await self._modify(remote_ref, add=[label_id], remove=remove)
        return remote_ref

    async def archive(self, remote_ref: str) -> None:
        await self.move(remote_ref, ALL_MAIL)

    async def apply_label(self, remote_ref: str, label: str) -> None:
        self._require_write()
        label_id = await self._label_id(label, create=True)
        assert label_id is not None
        await self._modify(remote_ref, add=[label_id])

    async def remove_label(self, remote_ref: str, label: str) -> None:
        self._require_write()
        label_id = await self._label_id(label)
        if label_id is not None:
            await self._modify(remote_ref, remove=[label_id])

    async def send(self, reply: OutgoingReply) -> SentMessage:
        self._require_write()
        body: dict[str, Any] = {"raw": base64.urlsafe_b64encode(reply.raw).decode("ascii")}
        if reply.provider_thread_id:
            body["threadId"] = reply.provider_thread_id
        try:
            data = await self._api.request("POST", "/messages/send", json=body, retry=False)
        except AuthenticationError as exc:
            if exc.code in {"insufficient_scope", "access_denied"}:
                raise SendError(code="send_not_permitted") from None
            raise
        except (BadRequestError, NotFoundError, ConflictError):
            raise SendError(code="message_refused") from None
        message_id = data.get("id")
        return SentMessage(
            remote_ref=message_id if isinstance(message_id, str) else None,
            message_id=reply.message_id,
        )

    async def aclose(self) -> None:
        if self._own_http:
            await self._http.aclose()


def _labels(message: dict[str, Any]) -> tuple[str, ...]:
    return tuple(str(label) for label in message.get("labelIds") or ())


def _notification_address(item: dict[str, Any]) -> str | None:
    """``emailAddress`` of a Gmail Pub/Sub notification (base64 JSON in ``data``)."""
    try:
        data = json.loads(base64.b64decode(item["message"]["data"]))
        return str(data["emailAddress"]).casefold()
    except (KeyError, TypeError, ValueError, binascii.Error):
        return None


registry.register(MailboxType.GMAIL, GmailProvider)
