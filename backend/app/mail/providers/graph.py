"""Microsoft 365 / Exchange Online ``MailProvider`` via Microsoft Graph
(docs/providers/microsoft365.md).

Settings (``Mailbox.provider_settings``, validated by ``GraphMailboxSettings``)::

    {"auth": "delegated" | "application", "user": "me" | "<address or user ID>",
     "tenant_id": "<optional>"}

Delegated mailboxes keep their OAuth tokens in ``Mailbox.credentials`` (``graph_auth``);
app-only mailboxes have no credentials, the Entra app comes from ``OLLAMAIL_MAIL_GRAPH_*``.

References: ``remote_ref`` is the immutable message ID (``Prefer: IdType="ImmutableId"``),
so it survives moves. Folder IDs are Graph folder IDs; ``RemoteFolder.name`` is the path.

Sync per folder with delta query, cursor ``{"v": 1, "link": <nextLink|deltaLink>,
"initial": bool}``. The initial round (``receivedDateTime ge since``) yields
``MessageFetched`` with the MIME source (``$value``, JSON-batched for messages without
attachments); every page ends with ``CursorAdvanced``, so an interrupted import resumes at
the last page. Later rounds yield ``MessageChanged`` (delta cannot tell new from changed;
the engine loads the source only for unknown messages). ``@removed`` is checked with a
GET: a message that still exists was moved (``MessageUpdated`` with its new folder),
otherwise it is deleted.

Sending (``send``): ``createReply`` (``createReplyAll``) on the answered message with the
recipients, subject and plain-text body of the draft, then ``send``; Exchange threads the
reply and keeps it in "Sent Items". Delegated mailboxes use a token with ``Mail.Send``
(requested when connecting, see ``graph_auth.delegated_scopes``); app-only access needs the
``Mail.Send`` application permission. Without it sending fails with ``send_not_permitted``.

Push: with ``OLLAMAIL_MAIL_GRAPH_NOTIFICATION_URL`` ``watch`` keeps a change-notification
subscription alive (the notifications themselves arrive at ``graph_router``); without it,
``watch`` raises ``NotImplementedError`` and the watcher polls.
"""

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import GraphSettings, MailSettings, SecuritySettings, get_settings
from app.core.logging import get_logger
from app.mail.models import FolderRole, MailboxType
from app.mail.providers.base import (
    AuthenticationError,
    ChangeEvent,
    ConfigurationError,
    ConnectionFailedError,
    CursorAdvanced,
    CursorInvalidError,
    Flag,
    MailboxConfig,
    MessageChanged,
    MessageDeleted,
    MessageFetched,
    MessageNotFoundError,
    MessageUpdated,
    OutgoingAddress,
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
from app.mail.providers.graph_auth import (
    MULTI_TENANT_AUTHORITIES,
    AppTokens,
    Clock,
    DelegatedTokens,
    TokenGetter,
)
from app.mail.providers.graph_client import (
    BatchRequest,
    GraphClient,
    GraphNotFoundError,
    Sleep,
    error_for,
)
from app.mail.providers.graph_webhook import client_state
from app.mail.providers.registry import registry

log = get_logger(__name__)

CURSOR_VERSION = 1
MESSAGE_FIELDS = (
    "id,parentFolderId,receivedDateTime,isRead,isDraft,flag,categories,conversationId,"
    "hasAttachments"
)
FOLDER_FIELDS = "id,displayName,parentFolderId,childFolderCount"
WELL_KNOWN_FOLDERS = {
    "inbox": FolderRole.INBOX,
    "sentitems": FolderRole.SENT,
    "drafts": FolderRole.DRAFTS,
    "deleteditems": FolderRole.TRASH,
    "junkemail": FolderRole.JUNK,
    "archive": FolderRole.ARCHIVE,
}
# Subscriptions on messages may live up to 10080 minutes; renewed after half the lifetime.
SUBSCRIPTION_LIFETIME = timedelta(days=2)
_FLAG_VALUES = frozenset(flag.value for flag in Flag)


class GraphMailboxSettings(BaseModel):
    """``Mailbox.provider_settings`` of a Microsoft 365 mailbox."""

    model_config = ConfigDict(extra="ignore")

    auth: Literal["delegated", "application"] = "delegated"
    # "me" (delegated, own mailbox), or the address/user ID of the mailbox to open.
    # Default: "me" for delegated, the mailbox address for app-only access.
    user: str | None = Field(default=None, max_length=320)
    # Tenant of the mailbox; default ``OLLAMAIL_MAIL_GRAPH_TENANT_ID``.
    tenant_id: str | None = Field(default=None, max_length=64)
    # Graph user ID (informational, set by the connect flow).
    user_id: str | None = Field(default=None, max_length=64)


def mailbox_path(settings: GraphMailboxSettings, address: str) -> str:
    """``/me`` or ``/users/{id}``, the root of all requests for this mailbox."""
    user = settings.user or ("me" if settings.auth == "delegated" else address)
    if user == "me":
        if settings.auth == "application":
            raise ConfigurationError()
        return "/me"
    return f"/users/{quote(user, safe='@')}"


@dataclass(slots=True)
class _Cursor:
    link: str
    initial: bool

    @classmethod
    def load(cls, cursor: SyncCursor) -> "_Cursor":
        data = cursor.data
        link = data.get("link")
        if data.get("v") != CURSOR_VERSION or not isinstance(link, str) or not link:
            raise CursorInvalidError()
        return cls(link=link, initial=bool(data.get("initial")))

    def dump(self) -> SyncCursor:
        return SyncCursor({"v": CURSOR_VERSION, "link": self.link, "initial": self.initial})


def _flags(item: dict[str, Any]) -> frozenset[str]:
    flags: set[str] = set()
    if item.get("isRead"):
        flags.add(Flag.SEEN)
    if item.get("isDraft"):
        flags.add(Flag.DRAFT)
    flag = item.get("flag")
    if isinstance(flag, dict) and flag.get("flagStatus") == "flagged":
        flags.add(Flag.FLAGGED)
    categories = item.get("categories")
    if isinstance(categories, list):
        flags.update(c for c in categories if isinstance(c, str) and c not in _FLAG_VALUES)
    return frozenset(flags)


def _categories(flags: Iterable[str]) -> list[str]:
    return sorted(flag for flag in flags if flag not in _FLAG_VALUES)


def _received_at(item: dict[str, Any]) -> datetime | None:
    value = item.get("receivedDateTime")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class GraphProvider:
    capabilities: ProviderCapabilities

    def __init__(
        self,
        config: MailboxConfig,
        *,
        settings: GraphSettings | None = None,
        mail_settings: MailSettings | None = None,
        security: SecuritySettings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = time.time,
    ) -> None:
        if settings is None or mail_settings is None or security is None:
            app_settings = get_settings()
            settings = settings or app_settings.graph
            mail_settings = mail_settings or app_settings.mail
            security = security or app_settings.security
        if not settings.enabled:
            raise ConfigurationError(code="graph_not_configured")
        try:
            self.mailbox_settings = GraphMailboxSettings.model_validate(config.settings)
        except ValidationError:
            raise ConfigurationError() from None
        self.config = config
        self.settings = settings
        self.security = security
        self.page_size = mail_settings.sync_batch_size
        self._sleep = sleep
        self._clock = clock
        self.base = mailbox_path(self.mailbox_settings, config.address)
        self.capabilities = ProviderCapabilities(
            labels=False,
            push=settings.notification_url is not None,
            server_threads=True,
            keywords=True,
        )
        tenant = self.mailbox_settings.tenant_id
        if self.mailbox_settings.auth == "application" and (
            (tenant or settings.tenant_id) in MULTI_TENANT_AUTHORITIES
        ):
            # Client credentials need the tenant that granted the admin consent.
            raise ConfigurationError(code="graph_tenant_required")
        self._auth_http = httpx.AsyncClient(timeout=settings.timeout, transport=transport)
        self._tenant = tenant
        self._transport = transport
        # Another user's mailbox needs the ``.Shared`` scopes.
        self._shared = (
            self.base != "/me"
            and (self.mailbox_settings.user or "").lower() != config.address.lower()
        )
        self.client = self._client(send=False)
        self._send_client: GraphClient | None = None

    def _client(self, *, send: bool) -> GraphClient:
        if self.mailbox_settings.auth == "application":
            # The app token carries every granted application permission (``.default``).
            token: TokenGetter = AppTokens(
                self.settings, self._auth_http, tenant=self._tenant, clock=self._clock
            )
        else:
            token = DelegatedTokens(
                self.settings,
                self._auth_http,
                self.config.credentials,
                tenant=self._tenant,
                shared=self._shared,
                send=send,
                save=self.config.save_credentials,
                clock=self._clock,
            )
        return GraphClient(
            api_url=self.settings.api_url,
            token=token,
            timeout=self.settings.timeout,
            max_retries=self.settings.max_retries,
            transport=self._transport,
            sleep=self._sleep,
        )

    async def aclose(self) -> None:
        await self.client.aclose()
        if self._send_client is not None:
            await self._send_client.aclose()
        await self._auth_http.aclose()

    async def verify(self) -> None:
        """Check access to the mailbox (connection test)."""
        await self.client.get_json(f"{self.base}/mailFolders/inbox", params={"$select": "id"})

    # -- folders --------------------------------------------------------------------------

    async def list_folders(self) -> list[RemoteFolder]:
        roles = await self._roles()
        folders: list[RemoteFolder] = []

        async def walk(path: str, parent_id: str | None, prefix: str) -> None:
            async for item in self.client.items(path, params={"$select": FOLDER_FIELDS}):
                folder_id = str(item["id"])
                name = str(item.get("displayName") or "")
                full_name = f"{prefix}/{name}" if prefix else name
                folders.append(
                    RemoteFolder(
                        remote_id=folder_id,
                        name=full_name,
                        role=roles.get(folder_id),
                        parent_id=parent_id,
                    )
                )
                if item.get("childFolderCount"):
                    await walk(
                        f"{self.base}/mailFolders/{quote(folder_id)}/childFolders",
                        folder_id,
                        full_name,
                    )

        await walk(f"{self.base}/mailFolders", None, "")
        return folders

    async def _roles(self) -> dict[str, FolderRole]:
        requests = [
            BatchRequest(id=name, url=f"{self.base}/mailFolders/{name}?$select=id")
            for name in WELL_KNOWN_FOLDERS
        ]
        roles: dict[str, FolderRole] = {}
        for name, response in (await self.client.batch(requests)).items():
            if response.status == 200 and isinstance(response.body, dict):
                folder_id = response.body.get("id")
                if isinstance(folder_id, str):
                    roles[folder_id] = WELL_KNOWN_FOLDERS[name]
            elif response.status in {401, 403}:
                raise error_for(response.status, response.body)
        return roles

    # -- sync -----------------------------------------------------------------------------

    async def fetch_since(
        self, folder_id: str, cursor: SyncCursor | None, *, since: datetime | None = None
    ) -> AsyncIterator[SyncEvent]:
        if cursor is None:
            params = {"$select": MESSAGE_FIELDS}
            if since is not None:
                params["$filter"] = f"receivedDateTime ge {_iso(since)}"
            path = f"{self.base}/mailFolders/{quote(folder_id)}/messages/delta"
            state = _Cursor(link=str(httpx.URL(self.client.url(path), params=params)), initial=True)
        else:
            state = _Cursor.load(cursor)
        prefer = (f"odata.maxpagesize={self.page_size}",)
        while True:
            try:
                page = await self.client.get_json(state.link, prefer=prefer)
            except GraphNotFoundError:
                raise ProviderError(code="folder_not_found") from None
            items = [item for item in page.get("value", []) if isinstance(item, dict)]
            if state.initial:
                for raw in await self._load_many(folder_id, items):
                    yield MessageFetched(raw, initial=True)
            else:
                async for event in self._changes(folder_id, items):
                    yield event
            next_link = page.get("@odata.nextLink")
            delta_link = page.get("@odata.deltaLink")
            if isinstance(next_link, str):
                state = _Cursor(link=next_link, initial=state.initial)
                yield CursorAdvanced(state.dump())
                continue
            if isinstance(delta_link, str):
                yield CursorAdvanced(_Cursor(link=delta_link, initial=False).dump())
                return
            raise ProviderError(code="invalid_response")

    async def _changes(
        self, folder_id: str, items: list[dict[str, Any]]
    ) -> AsyncIterator[SyncEvent]:
        for item in items:
            remote_ref = str(item.get("id") or "")
            if not remote_ref:
                continue
            if "@removed" in item:
                yield await self._removed(remote_ref)
                continue
            yield MessageChanged(
                remote_ref=remote_ref,
                load=_Loader(self, folder_id, item),
                flags=_flags(item),
                folder_ids=(str(item.get("parentFolderId") or folder_id),),
            )

    async def _removed(self, remote_ref: str) -> SyncEvent:
        """Deleted, or moved to another folder (immutable IDs stay the same)."""
        try:
            item = await self.client.get_json(
                f"{self.base}/messages/{quote(remote_ref)}", params={"$select": MESSAGE_FIELDS}
            )
        except GraphNotFoundError:
            return MessageDeleted(remote_ref)
        parent = item.get("parentFolderId")
        if not isinstance(parent, str):
            return MessageDeleted(remote_ref)
        return MessageUpdated(remote_ref, flags=_flags(item), folder_ids=(parent,))

    def _raw(self, folder_id: str, item: dict[str, Any], source: bytes) -> RawMessage:
        thread = item.get("conversationId")
        return RawMessage(
            remote_ref=str(item["id"]),
            raw=source,
            folder_ids=(str(item.get("parentFolderId") or folder_id),),
            flags=_flags(item),
            provider_thread_id=thread if isinstance(thread, str) else None,
            received_at=_received_at(item),
        )

    async def load_message(self, folder_id: str, item: dict[str, Any]) -> RawMessage:
        try:
            response = await self.client.request(
                "GET", f"{self.base}/messages/{quote(str(item['id']))}/$value"
            )
        except GraphNotFoundError:
            raise MessageNotFoundError() from None
        return self._raw(folder_id, item, response.content)

    async def _load_many(self, folder_id: str, items: list[dict[str, Any]]) -> list[RawMessage]:
        """MIME sources of ``items`` (skipping ``@removed`` and vanished messages), in
        order. Messages without attachments are batched, others fetched one by one."""
        items = [item for item in items if "@removed" not in item and item.get("id")]
        small = [item for item in items if not item.get("hasAttachments")]
        requests = [
            BatchRequest(id=str(index), url=f"{self.base}/messages/{quote(str(item['id']))}/$value")
            for index, item in enumerate(small)
        ]
        sources: dict[str, bytes] = {}
        for index, response in (await self.client.batch(requests)).items():
            item = small[int(index)]
            if response.status == 200:
                sources[str(item["id"])] = response.content()
            elif response.status != 404:
                raise error_for(response.status, response.body)
        result = []
        for item in items:
            source = sources.get(str(item["id"]))
            if source is None and item.get("hasAttachments"):
                with contextlib.suppress(MessageNotFoundError):
                    result.append(await self.load_message(folder_id, item))
                continue
            if source is not None:
                result.append(self._raw(folder_id, item, source))
        return result

    # -- push -----------------------------------------------------------------------------

    async def watch(self, folder_id: str | None = None) -> AsyncIterator[ChangeEvent]:
        """Keep a change-notification subscription for the whole mailbox alive. Yields one
        ``ChangeEvent`` per (re)subscription; notifications arrive at the API."""
        notification_url = self.settings.notification_url
        if notification_url is None:
            raise NotImplementedError("change notifications are not configured")
        subscription_id: str | None = None
        try:
            while True:
                if subscription_id is None:
                    subscription_id = await self._subscribe(notification_url)
                    yield ChangeEvent(None)
                await self._sleep(SUBSCRIPTION_LIFETIME.total_seconds() / 2)
                if not await self._renew(subscription_id):
                    subscription_id = None
        finally:
            if subscription_id is not None:
                await self._unsubscribe(subscription_id)

    def _expiration(self) -> str:
        return _iso(datetime.fromtimestamp(self._clock(), UTC) + SUBSCRIPTION_LIFETIME)

    async def _subscribe(self, notification_url: str) -> str:
        try:
            return await self._create_subscription(notification_url)
        except (ConnectionFailedError, NotImplementedError):
            raise
        except ProviderError as exc:
            # E.g. Graph could not validate the URL or the permission is missing: keep
            # polling instead of failing the mailbox.
            log.warning(
                "graph_subscription_failed", mailbox_id=str(self.config.mailbox_id), error=exc.code
            )
            raise NotImplementedError("change notifications unavailable") from None

    async def _create_subscription(self, notification_url: str) -> str:
        mailbox_id = self.config.mailbox_id
        if not isinstance(mailbox_id, uuid.UUID):
            mailbox_id = uuid.UUID(str(mailbox_id))
        response = await self.client.request(
            "POST",
            "/subscriptions",
            json_body={
                "changeType": "created,updated,deleted",
                "notificationUrl": notification_url,
                "lifecycleNotificationUrl": notification_url,
                "resource": f"{self.base.lstrip('/')}/messages",
                "expirationDateTime": self._expiration(),
                "clientState": client_state(self.security, mailbox_id),
                "latestSupportedTlsVersion": "v1_2",
            },
        )
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get("id"), str):
            raise ProviderError(code="invalid_response")
        log.info("graph_subscription_created", mailbox_id=str(mailbox_id))
        return str(data["id"])

    async def _renew(self, subscription_id: str) -> bool:
        try:
            await self.client.request(
                "PATCH",
                f"/subscriptions/{quote(subscription_id)}",
                json_body={"expirationDateTime": self._expiration()},
            )
        except GraphNotFoundError:
            return False
        return True

    async def _unsubscribe(self, subscription_id: str) -> None:
        try:
            async with asyncio.timeout(10):
                await self.client.request("DELETE", f"/subscriptions/{quote(subscription_id)}")
        except (ProviderError, TimeoutError):
            # Expires on its own; notifications for it are ignored after the mailbox is gone.
            pass

    # -- actions --------------------------------------------------------------------------

    def _message(self, remote_ref: str) -> str:
        return f"{self.base}/messages/{quote(remote_ref)}"

    async def _call(
        self, method: str, path: str, json_body: Any = None, params: dict[str, str] | None = None
    ) -> httpx.Response:
        try:
            return await self.client.request(method, path, json_body=json_body, params=params)
        except GraphNotFoundError:
            raise MessageNotFoundError() from None

    async def move(self, remote_ref: str, target_folder_id: str) -> str:
        response = await self._call(
            "POST", f"{self._message(remote_ref)}/move", {"destinationId": target_folder_id}
        )
        data = response.json()
        new_id = data.get("id") if isinstance(data, dict) else None
        return new_id if isinstance(new_id, str) else remote_ref

    async def set_flags(self, remote_ref: str, flags: frozenset[str]) -> None:
        await self._call(
            "PATCH",
            self._message(remote_ref),
            {
                "isRead": Flag.SEEN in flags,
                "flag": {"flagStatus": "flagged" if Flag.FLAGGED in flags else "notFlagged"},
                "categories": _categories(flags),
            },
        )

    async def _categories_of(self, remote_ref: str) -> list[str]:
        response = await self._call(
            "GET", self._message(remote_ref), params={"$select": "categories"}
        )
        data = response.json()
        categories = data.get("categories") if isinstance(data, dict) else None
        return [c for c in categories or [] if isinstance(c, str)]

    async def apply_label(self, remote_ref: str, label: str) -> None:
        categories = await self._categories_of(remote_ref)
        if label not in categories:
            await self._call(
                "PATCH", self._message(remote_ref), {"categories": [*categories, label]}
            )

    async def remove_label(self, remote_ref: str, label: str) -> None:
        categories = await self._categories_of(remote_ref)
        if label in categories:
            await self._call(
                "PATCH",
                self._message(remote_ref),
                {"categories": [c for c in categories if c != label]},
            )

    # -- sending --------------------------------------------------------------------------

    async def _send_request(self, path: str, json_body: Any = None) -> httpx.Response:
        if not self.settings.send_enabled:
            raise SendError(code="send_not_permitted")
        if self._send_client is None:
            self._send_client = self._client(send=True)
        try:
            return await self._send_client.request("POST", path, json_body=json_body, retry=False)
        except GraphNotFoundError:
            raise MessageNotFoundError() from None
        except AuthenticationError:
            # No Mail.Send consent (or app permission), or the grant was revoked.
            raise SendError(code="send_not_permitted") from None
        except ConnectionFailedError:
            raise
        except ProviderError as exc:
            raise SendError(code="message_refused") from exc

    async def send(self, reply: OutgoingReply) -> SentMessage:
        """``createReply``/``createReplyAll`` with the draft's content, then ``send``. The
        reply draft is removed again if sending fails."""
        if reply.in_reply_to_ref is None:
            raise SendError(code="original_missing")
        action = "createReplyAll" if reply.reply_all else "createReply"
        message = {
            "subject": reply.subject,
            "toRecipients": _recipients(reply.to),
            "ccRecipients": _recipients(reply.cc),
            "body": {"contentType": "text", "content": reply.body_text},
        }
        response = await self._send_request(
            f"{self._message(reply.in_reply_to_ref)}/{action}", {"message": message}
        )
        data = response.json()
        draft_id = data.get("id") if isinstance(data, dict) else None
        if not isinstance(draft_id, str):
            raise SendError(code="invalid_response")
        try:
            await self._send_request(f"{self._message(draft_id)}/send")
        except ProviderError:
            with contextlib.suppress(ProviderError):
                assert self._send_client is not None
                await self._send_client.request("DELETE", self._message(draft_id), retry=False)
            raise
        internet_id = data.get("internetMessageId")
        return SentMessage(
            remote_ref=draft_id,
            message_id=internet_id if isinstance(internet_id, str) else None,
        )


def _recipients(addresses: Iterable[OutgoingAddress]) -> list[dict[str, Any]]:
    return [
        {"emailAddress": {"address": a.address, **({"name": a.name} if a.name else {})}}
        for a in addresses
    ]


@dataclass(frozen=True, slots=True)
class _Loader:
    """``MessageChanged.load``: downloads the MIME source of one delta item."""

    provider: GraphProvider
    folder_id: str
    item: dict[str, Any]

    async def __call__(self) -> RawMessage:
        return await self.provider.load_message(self.folder_id, self.item)


registry.register(MailboxType.GRAPH, GraphProvider)
