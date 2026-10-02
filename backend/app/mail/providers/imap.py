"""IMAP ``MailProvider`` (docs/ARCHITECTURE.md §3.1).

Settings (``Mailbox.provider_settings``, validated by ``ImapSettings``) and credentials
(``Mailbox.credentials``, encrypted): ``{"password": ...}`` for ``auth="password"`` or
``{"access_token": ...}`` for ``auth="xoauth2"`` (refreshing the token is up to the OAuth
module of Graph/Gmail, see ``token_provider``).

Transport security: TLS (port 993) or STARTTLS with certificate verification. Unencrypted
connections and unverified certificates are refused unless the admin enables
``OLLAMAIL_MAIL_ALLOW_INSECURE_CONNECTIONS``.

Sending (``send``): SMTP submission (``smtp_*`` settings, default: the IMAP host with
STARTTLS on port 587, same login), then the message is appended to the sent folder
(special-use ``\\Sent`` or the usual names) with ``\\Seen``, unless ``smtp_save_sent`` is off
for servers that store sent mails themselves. The SMTP password is ``credentials
["smtp_password"]`` if set, else the IMAP password; with ``auth="xoauth2"`` the access token.

References: ``remote_ref`` is ``"<UIDVALIDITY>:<UID>:<folder>"``. Folder IDs are the
mailbox names as sent by the server (modified UTF-7), ``RemoteFolder.name`` is decoded.

Sync (``fetch_since``), cursor ``{"v": 1, "uidvalidity", "high", "modseq", "known",
"import"}``:

* ``known``: UIDs that were reported (``MessageFetched``) and not deleted, as compact
  sequence set. Deletions are detected against it.
* ``high``: highest UID that was looked at; newer UIDs are new messages.
* ``modseq``: ``HIGHESTMODSEQ`` at the last sync if the server supports CONDSTORE.
* ``import``: pending initial import ``{"since": date | None, "below": uid}``: UIDs below
  ``below`` received after ``since`` still have to be fetched.

Order of one sync: (1) changes of known messages: with QRESYNC one ``UID FETCH ...
(CHANGEDSINCE m VANISHED)``, with CONDSTORE ``CHANGEDSINCE`` plus a UID search for
deletions, otherwise all flags of the known range; (2) new messages, oldest first;
(3) the initial import, newest first. Every batch ends with ``CursorAdvanced``, so an
interrupted import continues where it stopped.
"""

import asyncio
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import MailSettings, get_settings
from app.core.logging import get_logger
from app.mail.models import FolderRole, MailboxType
from app.mail.providers import smtp
from app.mail.providers.base import (
    AuthenticationError,
    ChangeEvent,
    ConfigurationError,
    ConnectionFailedError,
    CursorAdvanced,
    CursorInvalidError,
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
    SentMessage,
    SyncCursor,
    SyncEvent,
)
from app.mail.providers.imap_client import (
    Arg,
    CommandResult,
    ImapCommandError,
    ImapConnection,
    ImapConnectionError,
    ImapError,
    Literal_,
    SelectInfo,
)
from app.mail.providers.imap_protocol import (
    UidSet,
    Untagged,
    as_int,
    decode_mailbox,
    encode_keyword,
    encode_mailbox,
    fetch_items,
    flags_from_imap,
    flags_to_imap,
    format_search_date,
    parse_internaldate,
)
from app.mail.providers.registry import registry

log = get_logger(__name__)

CURSOR_VERSION = 1
# Bytes of message sources fetched per round trip (bounds memory per batch).
FETCH_BYTES = 16 * 1024 * 1024
# IDLE is re-issued well before the 29-minute server timeout (RFC 2177); a short cycle also
# detects connections that NAT gateways dropped silently.
IDLE_RESTART_SECONDS = 10 * 60
INBOX = "INBOX"
# Created for sent mails if the server has no sent folder.
SENT_FOLDER = "Sent"

# Special-use attributes (RFC 6154) and common names as fallback.
_SPECIAL_USE = {
    "\\SENT": FolderRole.SENT,
    "\\DRAFTS": FolderRole.DRAFTS,
    "\\TRASH": FolderRole.TRASH,
    "\\JUNK": FolderRole.JUNK,
    "\\ARCHIVE": FolderRole.ARCHIVE,
    "\\ALL": FolderRole.ALL,
}
_ROLE_NAMES = {
    FolderRole.SENT: {
        "sent",
        "sent items",
        "sent messages",
        "gesendet",
        "gesendete elemente",
        "gesendete objekte",
    },
    FolderRole.DRAFTS: {"drafts", "entwürfe", "entwurf"},
    FolderRole.TRASH: {
        "trash",
        "deleted items",
        "deleted messages",
        "papierkorb",
        "gelöschte elemente",
        "gelöschte objekte",
        "bin",
    },
    FolderRole.JUNK: {"junk", "spam", "junk e-mail", "junk-e-mail", "bulk mail"},
    FolderRole.ARCHIVE: {"archive", "archiv"},
}

TokenProvider = Callable[[MailboxConfig], Awaitable[str]]


class ImapSettings(BaseModel):
    """``Mailbox.provider_settings`` of an IMAP mailbox."""

    model_config = ConfigDict(extra="ignore")

    host: str = Field(min_length=1, max_length=255)
    # Default: 993 for ``tls``, 143 otherwise.
    port: int | None = Field(default=None, ge=1, le=65535)
    # ``tls`` (implicit TLS), ``starttls`` or ``none`` (admin flag required).
    security: Literal["tls", "starttls", "none"] = "tls"
    # ``False`` accepts any certificate (admin flag required).
    verify_certificate: bool = True
    auth: Literal["password", "xoauth2"] = "password"
    # Login name; default: the mailbox address.
    username: str | None = None
    # SMTP submission for sending replies. Host default: the IMAP host.
    smtp_host: str | None = Field(default=None, min_length=1, max_length=255)
    # Default: 465 for ``tls``, 587 for ``starttls``, 25 for ``none``.
    smtp_port: int | None = Field(default=None, ge=1, le=65535)
    # ``tls``, ``starttls`` or ``none`` (admin flag required); certificate check as for IMAP.
    smtp_security: Literal["tls", "starttls", "none"] = "starttls"
    # SMTP login name; default: the IMAP login name.
    smtp_username: str | None = Field(default=None, max_length=320)
    # Append sent mails to the sent folder (off if the server does it itself).
    smtp_save_sent: bool = True


def make_ref(folder: str, uidvalidity: int, uid: int) -> str:
    return f"{uidvalidity}:{uid}:{folder}"


def parse_ref(remote_ref: str) -> tuple[str, int, int]:
    try:
        uidvalidity, uid, folder = remote_ref.split(":", 2)
        return folder, int(uidvalidity), int(uid)
    except ValueError:
        raise MessageNotFoundError() from None


def _wire(folder: str) -> bytes:
    # Folder IDs are the server's names, decoded as Latin-1 so the bytes round-trip.
    return folder.encode("latin-1")


def _folder_id(raw: bytes) -> str:
    return raw.decode("latin-1")


@dataclass(slots=True)
class _Cursor:
    uidvalidity: int
    high: int
    modseq: int | None = None
    known: UidSet = field(default_factory=UidSet)
    import_since: date | None = None
    import_below: int | None = None

    @classmethod
    def load(cls, cursor: SyncCursor) -> "_Cursor":
        data = cursor.data
        try:
            if data.get("v") != CURSOR_VERSION:
                raise ValueError
            pending = data.get("import")
            since = pending.get("since") if pending else None
            return cls(
                uidvalidity=int(data["uidvalidity"]),
                high=int(data["high"]),
                modseq=int(data["modseq"]) if data.get("modseq") is not None else None,
                known=UidSet.parse(str(data.get("known", ""))),
                import_since=date.fromisoformat(since) if since else None,
                import_below=int(pending["below"]) if pending else None,
            )
        except (KeyError, TypeError, ValueError):
            raise CursorInvalidError() from None

    def dump(self) -> SyncCursor:
        data: dict[str, Any] = {
            "v": CURSOR_VERSION,
            "uidvalidity": self.uidvalidity,
            "high": self.high,
            "modseq": self.modseq,
            "known": str(self.known),
        }
        if self.import_below is not None:
            data["import"] = {
                "since": self.import_since.isoformat() if self.import_since else None,
                "below": self.import_below,
            }
        return SyncCursor(data)


def _batched(items: list[int], size: int) -> Iterator[list[int]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


class ImapProvider:
    capabilities = ProviderCapabilities(
        labels=False, push=True, server_threads=False, keywords=True
    )

    def __init__(
        self,
        config: MailboxConfig,
        *,
        mail_settings: MailSettings | None = None,
        token_provider: TokenProvider | None = None,
        batch_size: int | None = None,
        disabled_extensions: Iterable[str] = (),
    ) -> None:
        """``disabled_extensions`` (e.g. ``{"QRESYNC"}``) ignores server extensions; for
        tests of the fallback paths and for working around broken servers."""
        instance = mail_settings or get_settings().mail
        try:
            self.settings = ImapSettings.model_validate(config.settings)
        except ValidationError:
            raise ConfigurationError() from None
        insecure = self.settings.security == "none" or not self.settings.verify_certificate
        if insecure and not instance.allow_insecure_connections:
            raise ConfigurationError(code="insecure_connection_refused")
        self.config = config
        self._allow_insecure = instance.allow_insecure_connections
        self._timeout = instance.imap_timeout
        self._batch_size = batch_size or instance.sync_batch_size
        self._token_provider = token_provider
        self._disabled = frozenset(e.upper() for e in disabled_extensions)
        self._conn: ImapConnection | None = None
        self._lock = asyncio.Lock()

    # -- connection -------------------------------------------------------------------

    def _ssl_context(self) -> ssl.SSLContext | None:
        if self.settings.security == "none":
            return None
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        if not self.settings.verify_certificate:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context

    @property
    def _port(self) -> int:
        if self.settings.port is not None:
            return self.settings.port
        return 993 if self.settings.security == "tls" else 143

    def _has(self, conn: ImapConnection, extension: str) -> bool:
        return extension in conn.capabilities and extension not in self._disabled

    async def _open(self) -> ImapConnection:
        conn = await ImapConnection.open(
            self.settings.host,
            self._port,
            security=self.settings.security,
            ssl_context=self._ssl_context(),
            connect_timeout=min(self._timeout, 30.0),
            command_timeout=self._timeout,
        )
        try:
            await self._authenticate(conn)
            if self._has(conn, "ENABLE"):
                if self._has(conn, "QRESYNC") and self._has(conn, "CONDSTORE"):
                    await conn.enable("QRESYNC")
                elif self._has(conn, "CONDSTORE"):
                    await conn.enable("CONDSTORE")
        except BaseException:
            await conn.close(logout=False)
            raise
        return conn

    async def _access_token(self) -> str:
        token: object = (
            await self._token_provider(self.config)
            if self._token_provider is not None
            else self.config.credentials.get("access_token")
        )
        if not isinstance(token, str) or not token:
            raise AuthenticationError(code="credentials_missing")
        return token

    async def _authenticate(self, conn: ImapConnection) -> None:
        username = self.settings.username or self.config.address
        credentials = self.config.credentials
        try:
            if self.settings.auth == "xoauth2":
                await conn.authenticate_xoauth2(username, await self._access_token())
            else:
                password = credentials.get("password")
                if not isinstance(password, str):
                    raise AuthenticationError(code="credentials_missing")
                await conn.login(username, password)
        except ImapCommandError:
            raise AuthenticationError() from None

    async def _connection(self) -> ImapConnection:
        if self._conn is None or self._conn.closed:
            self._conn = await self._open()
        return self._conn

    @contextmanager
    def _errors(self) -> Iterator[None]:
        """Translate client errors into ``ProviderError``s (no server data)."""
        try:
            yield
        except ImapConnectionError as exc:
            self._conn = None
            raise ConnectionFailedError(code=_connection_code(exc.reason)) from None
        except ImapCommandError as exc:
            raise ProviderError(code="server_error") from exc
        except ImapError as exc:
            self._conn = None
            raise ProviderError(code="protocol_error") from exc

    async def verify(self) -> None:
        """Connection test: connect, authenticate and list the folders."""
        await self.list_folders()

    async def aclose(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            await conn.close()

    async def _select(
        self, conn: ImapConnection, folder: str, *, read_only: bool, force: bool = False
    ) -> SelectInfo | None:
        """Select ``folder`` unless it is selected already (then ``None``)."""
        selected = conn.selected
        same = not force and selected is not None and selected[0] == _wire(folder)
        if same and selected is not None and (read_only or not selected[1]):
            return None
        return await conn.select(_wire(folder), read_only=read_only)

    # -- folders ----------------------------------------------------------------------

    async def list_folders(self) -> list[RemoteFolder]:
        async with self._lock:
            with self._errors():
                conn = await self._connection()
                if self._has(conn, "SPECIAL-USE") and self._has(conn, "LIST-EXTENDED"):
                    result = await conn.command("LIST", b"", b"*", "RETURN (SPECIAL-USE)")
                else:
                    result = await conn.command("LIST", b"", b"*")
        return _folders(result.of_kind("LIST"))

    # -- sync -------------------------------------------------------------------------

    async def fetch_since(
        self, folder_id: str, cursor: SyncCursor | None, *, since: datetime | None = None
    ) -> AsyncIterator[SyncEvent]:
        with self._errors():
            conn = await self._connection()
            info = await self._select(conn, folder_id, read_only=True, force=True)
            assert info is not None
            use_modseq = self._has(conn, "CONDSTORE") and info.highestmodseq is not None
            current_high = (info.uidnext - 1) if info.uidnext else await self._max_uid(conn)

            if cursor is None or not cursor.data:
                state = _Cursor(
                    uidvalidity=info.uidvalidity,
                    high=current_high,
                    modseq=info.highestmodseq if use_modseq else None,
                    import_since=since.date() if since else None,
                    import_below=current_high + 1,
                )
            else:
                state = _Cursor.load(cursor)
                if state.uidvalidity != info.uidvalidity:
                    raise CursorInvalidError()
                async for event in self._changes(conn, folder_id, state, info, use_modseq):
                    yield event
                state.modseq = info.highestmodseq if use_modseq else None
                yield CursorAdvanced(state.dump())

                async for event in self._new_messages(conn, folder_id, state, info):
                    yield event
                state.high = max(state.high, current_high)

            if state.import_below is not None:
                async for event in self._import(conn, folder_id, state, info):
                    yield event
                state.import_below = None
            yield CursorAdvanced(state.dump())

    async def _reselect(self, folder: str, uidvalidity: int) -> ImapConnection:
        """The connection with ``folder`` selected again, in case the caller ran other
        operations (or the connection was renewed) while iterating ``fetch_since``."""
        conn = await self._connection()
        info = await self._select(conn, folder, read_only=True)
        if info is not None and info.uidvalidity != uidvalidity:
            raise CursorInvalidError()
        return conn

    async def _max_uid(self, conn: ImapConnection) -> int:
        """Highest UID of the selected folder, for servers without ``UIDNEXT``."""
        uids = await self._search(conn, "ALL")
        return max(uids, default=0)

    async def _search(self, conn: ImapConnection, *criteria: Arg) -> list[int]:
        if self._has(conn, "ESEARCH"):
            result = await conn.command("UID SEARCH RETURN (ALL)", *criteria)
            uids: list[int] = []
            for response in result.of_kind("ESEARCH"):
                values = list(response.values)
                for index, value in enumerate(values[:-1]):
                    if isinstance(value, str) and value.upper() == "ALL":
                        uids.extend(UidSet.parse(str(values[index + 1])))
            return uids
        result = await conn.command("UID SEARCH", *criteria)
        return [
            int(value)
            for response in result.of_kind("SEARCH")
            for value in response.values
            if isinstance(value, str) and value.isdigit()
        ]

    async def _changes(
        self,
        conn: ImapConnection,
        folder: str,
        state: _Cursor,
        info: SelectInfo,
        use_modseq: bool,
    ) -> AsyncIterator[SyncEvent]:
        """Flag changes and deletions of known messages."""
        if not state.known:
            return
        span = f"{state.known.min}:{state.known.max}"
        incremental = use_modseq and state.modseq is not None
        if incremental and self._has(conn, "QRESYNC") and "QRESYNC" in conn.enabled:
            result = await conn.command(
                "UID FETCH", span, "(UID FLAGS)", f"(CHANGEDSINCE {state.modseq} VANISHED)"
            )
            vanished = _vanished(result.of_kind("VANISHED"))
        elif incremental:
            result = await conn.command(
                "UID FETCH", span, "(UID FLAGS)", f"(CHANGEDSINCE {state.modseq})"
            )
            present = UidSet.of(await self._search(conn, "UID", span))
            vanished = UidSet.of(uid for uid in state.known if uid not in present)
        else:
            result = await conn.command("UID FETCH", span, "(UID FLAGS)")
            present = UidSet.of(uid for uid, _ in _flag_updates(result))
            vanished = UidSet.of(uid for uid in state.known if uid not in present)

        deleted = [uid for uid in state.known if uid in vanished]
        for uid in deleted:
            yield MessageDeleted(make_ref(folder, info.uidvalidity, uid))
        for uid, flags in _flag_updates(result):
            if uid in state.known and uid not in vanished:
                yield MessageUpdated(make_ref(folder, info.uidvalidity, uid), flags=flags)
        state.known = state.known.difference(deleted)

    async def _new_messages(
        self, conn: ImapConnection, folder: str, state: _Cursor, info: SelectInfo
    ) -> AsyncIterator[SyncEvent]:
        uids = sorted(
            uid
            for uid in await self._search(conn, "UID", f"{state.high + 1}:*")
            if uid > state.high
        )
        for batch in _batched(uids, self._batch_size):
            conn = await self._reselect(folder, info.uidvalidity)
            async for message in self._fetch(conn, folder, info.uidvalidity, batch):
                yield MessageFetched(message)
            state.known = state.known.union(batch)
            state.high = max(state.high, batch[-1])
            yield CursorAdvanced(state.dump())

    async def _import(
        self, conn: ImapConnection, folder: str, state: _Cursor, info: SelectInfo
    ) -> AsyncIterator[SyncEvent]:
        """Initial import below ``state.import_below``, newest (highest UID) first."""
        assert state.import_below is not None
        if state.import_below <= 1:
            return
        criteria = ["UID", f"1:{state.import_below - 1}"]
        if state.import_since is not None:
            criteria += ["SINCE", format_search_date(state.import_since)]
        uids = sorted(
            (uid for uid in await self._search(conn, *criteria) if uid < state.import_below),
            reverse=True,
        )
        for batch in _batched(uids, self._batch_size):
            conn = await self._reselect(folder, info.uidvalidity)
            async for message in self._fetch(conn, folder, info.uidvalidity, batch):
                yield MessageFetched(message, initial=True)
            state.known = state.known.union(batch)
            state.import_below = batch[-1]
            yield CursorAdvanced(state.dump())

    async def _fetch(
        self, conn: ImapConnection, folder: str, uidvalidity: int, uids: list[int]
    ) -> AsyncIterator[RawMessage]:
        """Fetch message sources in the order of ``uids``, in size-bounded round trips."""
        result = await conn.command("UID FETCH", str(UidSet.of(uids)), "(UID RFC822.SIZE)")
        sizes: dict[int, int] = {}
        for response in result.of_kind("FETCH"):
            items = fetch_items(response)
            uid = as_int(items.get("UID"))
            if uid is not None:
                sizes[uid] = as_int(items.get("RFC822.SIZE")) or 0
        group: list[int] = []
        group_bytes = 0
        for uid in [u for u in uids if u in sizes]:
            if group and group_bytes + sizes[uid] > FETCH_BYTES:
                async for message in self._fetch_group(conn, folder, uidvalidity, group):
                    yield message
                group, group_bytes = [], 0
            group.append(uid)
            group_bytes += sizes[uid]
        if group:
            async for message in self._fetch_group(conn, folder, uidvalidity, group):
                yield message

    async def _fetch_group(
        self, conn: ImapConnection, folder: str, uidvalidity: int, uids: list[int]
    ) -> AsyncIterator[RawMessage]:
        result = await conn.command(
            "UID FETCH", str(UidSet.of(uids)), "(UID FLAGS INTERNALDATE BODY.PEEK[])"
        )
        messages: dict[int, RawMessage] = {}
        for response in result.of_kind("FETCH"):
            items = fetch_items(response)
            uid = as_int(items.get("UID"))
            source = items.get("BODY[]")
            if uid is None or not isinstance(source, bytes):
                continue
            flags = items.get("FLAGS")
            messages[uid] = RawMessage(
                remote_ref=make_ref(folder, uidvalidity, uid),
                raw=source,
                folder_ids=(folder,),
                flags=flags_from_imap(flags if isinstance(flags, list) else []),
                received_at=parse_internaldate(items.get("INTERNALDATE")),
            )
        for uid in uids:
            if uid in messages:
                yield messages.pop(uid)

    # -- push -------------------------------------------------------------------------

    async def watch(self, folder_id: str | None = None) -> AsyncIterator[ChangeEvent]:
        """IMAP ``IDLE`` on ``folder_id`` (default: INBOX) on a dedicated connection."""
        folder = folder_id or INBOX
        with self._errors():
            conn = await self._open()
            try:
                if not self._has(conn, "IDLE"):
                    raise NotImplementedError("server does not support IDLE")
                await conn.select(_wire(folder), read_only=True)
                while True:
                    if await conn.idle(IDLE_RESTART_SECONDS):
                        yield ChangeEvent(folder)
            finally:
                await conn.close()

    # -- actions ----------------------------------------------------------------------

    async def _locate(self, conn: ImapConnection, remote_ref: str) -> tuple[str, int, SelectInfo]:
        """Select the message's folder read-write and check that the message exists."""
        folder, uidvalidity, uid = parse_ref(remote_ref)
        try:
            info = await conn.select(_wire(folder), read_only=False)
        except ImapCommandError:
            raise MessageNotFoundError() from None
        if info.uidvalidity != uidvalidity:
            raise MessageNotFoundError()
        result = await conn.command("UID FETCH", str(uid), "(UID)")
        if not any(as_int(fetch_items(r).get("UID")) == uid for r in result.of_kind("FETCH")):
            raise MessageNotFoundError()
        return folder, uid, info

    async def set_flags(self, remote_ref: str, flags: frozenset[str]) -> None:
        async with self._lock:
            with self._errors():
                conn = await self._connection()
                _, uid, _ = await self._locate(conn, remote_ref)
                imap_flags = " ".join(flags_to_imap(flags))
                await conn.command("UID STORE", str(uid), "FLAGS.SILENT", f"({imap_flags})")

    async def move(self, remote_ref: str, target_folder_id: str) -> str:
        async with self._lock:
            with self._errors():
                conn = await self._connection()
                _, uid, _ = await self._locate(conn, remote_ref)
                target = _wire(target_folder_id)
                if self._has(conn, "MOVE"):
                    result = await conn.command("UID MOVE", str(uid), target)
                elif self._has(conn, "UIDPLUS"):
                    result = await conn.command("UID COPY", str(uid), target)
                    await conn.command("UID STORE", str(uid), "+FLAGS.SILENT", "(\\Deleted)")
                    await conn.command("UID EXPUNGE", str(uid))
                else:
                    raise ProviderError(code="unsupported_operation")
                copied = _copyuid(result, uid)
                if copied is None:
                    raise ProviderError(code="move_result_unknown")
                return make_ref(target_folder_id, *copied)

    async def apply_label(self, remote_ref: str, label: str) -> None:
        """Set ``label`` as keyword, or copy into a folder named ``label`` if the folder
        does not accept keywords."""
        async with self._lock:
            with self._errors():
                conn = await self._connection()
                _, uid, info = await self._locate(conn, remote_ref)
                if "\\*" in info.permanent_flags:
                    keyword = _keyword(label)
                    await conn.command("UID STORE", str(uid), "+FLAGS.SILENT", f"({keyword})")
                    return
                target = _label_folder(label)
                try:
                    await conn.command("CREATE", target)
                except ImapCommandError as exc:
                    if exc.code != "ALREADYEXISTS" and not await self._exists(conn, target):
                        raise
                await conn.command("UID COPY", str(uid), target)

    async def remove_label(self, remote_ref: str, label: str) -> None:
        async with self._lock:
            with self._errors():
                conn = await self._connection()
                _, uid, info = await self._locate(conn, remote_ref)
                if "\\*" in info.permanent_flags:
                    keyword = _keyword(label)
                    await conn.command("UID STORE", str(uid), "-FLAGS.SILENT", f"({keyword})")
                    return
                message_id = await self._message_id(conn, uid)
                target = _label_folder(label)
                if message_id is None or not await self._exists(conn, target):
                    return
                await conn.select(target, read_only=False)
                copies = await self._search(conn, "HEADER", "MESSAGE-ID", message_id.encode())
                if not copies:
                    return
                uid_set = str(UidSet.of(copies))
                await conn.command("UID STORE", uid_set, "+FLAGS.SILENT", "(\\Deleted)")
                if self._has(conn, "UIDPLUS"):
                    await conn.command("UID EXPUNGE", uid_set)

    # -- sending ----------------------------------------------------------------------

    async def _smtp_target(self) -> smtp.SmtpTarget:
        settings = self.settings
        insecure = settings.smtp_security == "none" or not settings.verify_certificate
        if insecure and not self._allow_insecure:
            raise ConfigurationError(code="insecure_connection_refused")
        if settings.auth == "xoauth2":
            secret = await self._access_token()
        else:
            password = self.config.credentials.get("smtp_password") or self.config.credentials.get(
                "password"
            )
            if not isinstance(password, str) or not password:
                raise AuthenticationError(code="credentials_missing")
            secret = password
        return smtp.SmtpTarget(
            host=settings.smtp_host or settings.host,
            port=settings.smtp_port or smtp.DEFAULT_PORTS[settings.smtp_security],
            security=settings.smtp_security,
            verify_certificate=settings.verify_certificate,
            username=settings.smtp_username or settings.username or self.config.address,
            auth=settings.auth,
            secret=secret,
            timeout=self._timeout,
        )

    async def send(self, reply: OutgoingReply) -> SentMessage:
        """Submit via SMTP, then store a copy in the sent folder. A failed copy does not
        fail the send (the mail is out): it is reported in ``sent_copy_error``."""
        target = await self._smtp_target()
        refused = await smtp.submit(target, self.config.address, reply.recipients, reply.raw)
        if not self.settings.smtp_save_sent:
            return SentMessage(message_id=reply.message_id, refused=refused)
        try:
            remote_ref = await self._append_sent(reply.raw)
        except ProviderError as exc:
            log.warning("imap_sent_copy_failed", error=exc.code)
            return SentMessage(
                message_id=reply.message_id, refused=refused, sent_copy_error=exc.code
            )
        return SentMessage(remote_ref=remote_ref, message_id=reply.message_id, refused=refused)

    async def _append_sent(self, raw: bytes) -> str | None:
        folders = await self.list_folders()
        sent = next((f.remote_id for f in folders if f.role is FolderRole.SENT), None)
        async with self._lock:
            with self._errors():
                conn = await self._connection()
                if sent is None:
                    sent = SENT_FOLDER
                    target = _wire(sent)
                    try:
                        await conn.command("CREATE", target)
                    except ImapCommandError as exc:
                        if exc.code != "ALREADYEXISTS" and not await self._exists(conn, target):
                            raise
                result = await conn.command("APPEND", _wire(sent), "(\\Seen)", Literal_(raw))
                appended = _appenduid(result)
                return make_ref(sent, *appended) if appended is not None else None

    async def _message_id(self, conn: ImapConnection, uid: int) -> str | None:
        result = await conn.command(
            "UID FETCH", str(uid), "(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])"
        )
        for response in result.of_kind("FETCH"):
            for key, value in fetch_items(response).items():
                if key.startswith("BODY[") and isinstance(value, bytes):
                    _, _, header = value.decode("ascii", "replace").partition(":")
                    return header.strip() or None
        return None

    async def _exists(self, conn: ImapConnection, folder: bytes) -> bool:
        result = await conn.command("LIST", b"", folder)
        return bool(result.of_kind("LIST"))


registry.register(MailboxType.IMAP, ImapProvider)


# --- helpers ----------------------------------------------------------------------------


def _connection_code(reason: str) -> str:
    if reason.startswith("tls"):
        return reason if reason == "tls_certificate_invalid" else "tls_failed"
    if reason == "starttls_unsupported":
        return reason
    return "connection_failed"


def _keyword(label: str) -> str:
    try:
        return encode_keyword(label)
    except ValueError:
        raise ProviderError(code="invalid_label") from None


def _label_folder(label: str) -> bytes:
    return encode_mailbox(label)


def _flag_updates(result: CommandResult) -> list[tuple[int, frozenset[str]]]:
    updates = []
    for response in result.of_kind("FETCH"):
        items = fetch_items(response)
        uid = as_int(items.get("UID"))
        flags = items.get("FLAGS")
        if uid is not None and isinstance(flags, list):
            updates.append((uid, flags_from_imap(flags)))
    return updates


def _vanished(responses: list[Untagged]) -> UidSet:
    uids = UidSet()
    for response in responses:
        for value in response.values:
            if isinstance(value, str):
                uids = uids.union(UidSet.parse(value))
    return uids


def _copyuid(result: CommandResult, uid: int) -> tuple[int, int] | None:
    """Target ``(UIDVALIDITY, UID)`` of ``uid`` from a ``COPYUID`` response code."""
    for code in result.codes():
        if code.name != "COPYUID" or len(code.args) != 3:
            continue
        validity, source, target = code.args
        uidvalidity = as_int(validity)
        if uidvalidity is None or not isinstance(source, str) or not isinstance(target, str):
            continue
        sources, targets = list(_ordered(source)), list(_ordered(target))
        if uid in sources and len(sources) == len(targets):
            return uidvalidity, targets[sources.index(uid)]
    return None


def _appenduid(result: CommandResult) -> tuple[int, int] | None:
    """``(UIDVALIDITY, UID)`` of an appended message from ``APPENDUID`` (UIDPLUS)."""
    for code in result.codes():
        if code.name == "APPENDUID" and len(code.args) == 2:
            uidvalidity, uid = (as_int(value) for value in code.args)
            if uidvalidity is not None and uid is not None:
                return uidvalidity, uid
    return None


def _ordered(sequence_set: str) -> Iterator[int]:
    """UIDs of a ``COPYUID`` set in the given order (it may be unsorted)."""
    for part in sequence_set.split(","):
        start, _, end = part.partition(":")
        first, last = int(start), int(end or start)
        step = 1 if last >= first else -1
        yield from range(first, last + step, step)


def _folders(responses: list[Untagged]) -> list[RemoteFolder]:
    entries: list[tuple[str, str | None, frozenset[str]]] = []
    for response in responses:
        if len(response.values) < 3:
            continue
        attributes, delimiter, name = response.values[:3]
        if isinstance(name, str):
            name = name.encode("latin-1")
        if not isinstance(name, bytes) or not isinstance(attributes, list):
            continue
        flags = frozenset(a.upper() for a in attributes if isinstance(a, str))
        if flags & {"\\NOSELECT", "\\NONEXISTENT"}:
            continue
        separator = delimiter.decode("latin-1") if isinstance(delimiter, bytes) else None
        entries.append((_folder_id(name), separator, flags))

    ids = {folder_id for folder_id, _, _ in entries}
    folders: list[RemoteFolder] = []
    for folder_id, separator, flags in entries:
        parent = None
        leaf = folder_id
        if separator and separator in folder_id:
            parent_name, _, leaf = folder_id.rpartition(separator)
            parent = parent_name if parent_name in ids else None
        role = FolderRole.INBOX if folder_id.upper() == INBOX else None
        role = role or next((r for a, r in _SPECIAL_USE.items() if a in flags), None)
        folders.append(
            RemoteFolder(
                remote_id=folder_id,
                name=decode_mailbox(leaf.encode("latin-1")),
                role=role,
                parent_id=parent,
            )
        )
    return _guess_roles(folders)


def _guess_roles(folders: list[RemoteFolder]) -> list[RemoteFolder]:
    """Assign roles by name for servers without special-use attributes."""
    taken = {folder.role for folder in folders if folder.role is not None}
    result = []
    for folder in folders:
        if folder.role is None and folder.parent_id is None:
            name = folder.name.casefold()
            role = next(
                (r for r, names in _ROLE_NAMES.items() if r not in taken and name in names),
                None,
            )
            if role is not None:
                taken.add(role)
                folder = replace(folder, role=role)
        result.append(folder)
    return result
