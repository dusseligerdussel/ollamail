"""Provider-independent mail access (docs/ARCHITECTURE.md §3.1).

One interface covers IMAP, Microsoft Graph and the Gmail API. The differences are
absorbed by the data types:

* **Folders vs. labels.** ``RemoteFolder.kind`` says whether a message lives in exactly
  one folder (IMAP, Graph) or can carry several labels (Gmail). ``RawMessage.folder_ids``
  lists all folders/labels of a message.
* **Change tracking.** ``SyncCursor`` is an opaque, JSON-serialisable state owned by the
  provider: IMAP ``UIDVALIDITY``/last UID/``HIGHESTMODSEQ``, a Graph ``deltaLink``, a Gmail
  ``historyId``. ``fetch_since`` yields changes and finally ``CursorAdvanced``; callers
  persist the cursor only after the preceding changes are stored, so syncing is resumable
  and idempotent.
* **Push.** ``watch`` wraps IMAP ``IDLE``, Graph change notifications and Gmail Pub/Sub. It
  only signals *that* something changed; the caller then runs ``fetch_since``. Providers
  without push raise ``NotImplementedError`` and the caller falls back to polling.
* **Content.** Every provider delivers the RFC 5322 source (IMAP ``BODY[]``, Graph
  ``/messages/{id}/$value``, Gmail ``format=raw``), so parsing and normalisation are shared
  (``app.mail.mime``).
"""

import enum
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from app.mail.models import FolderKind, FolderRole, MailboxType


class Flag(enum.StrEnum):
    """Provider-independent message flags. Other values in a flag set are server
    keywords (IMAP keywords, Gmail user labels are folders instead)."""

    SEEN = "seen"
    ANSWERED = "answered"
    FLAGGED = "flagged"
    DRAFT = "draft"
    DELETED = "deleted"


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    # A message can be in several folders at once (Gmail labels).
    labels: bool = False
    # ``watch`` is supported (IMAP IDLE, Graph notifications, Gmail Pub/Sub).
    push: bool = False
    # ``RawMessage.provider_thread_id`` is set (Gmail threadId, Graph conversationId).
    server_threads: bool = False
    # Arbitrary keywords can be stored in the flag set (IMAP ``PERMANENTFLAGS \\*``).
    keywords: bool = False


@dataclass(frozen=True, slots=True)
class MailboxConfig:
    """Everything a provider needs to connect. ``credentials`` are already decrypted and
    must never be logged."""

    mailbox_id: Any
    type: MailboxType
    address: str
    settings: dict[str, Any] = field(default_factory=dict)
    credentials: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True)
class RemoteFolder:
    remote_id: str
    name: str
    kind: FolderKind = FolderKind.FOLDER
    role: FolderRole | None = None
    # Remote ID of the parent folder, if the server has a hierarchy.
    parent_id: str | None = None


@dataclass(frozen=True, slots=True)
class SyncCursor:
    """Opaque provider state; ``data`` must be JSON-serialisable (``SyncState.cursor``)."""

    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RawMessage:
    # Stable provider reference for actions (IMAP: folder, UIDVALIDITY and UID; Graph and
    # Gmail: message ID). Unique per mailbox.
    remote_ref: str
    # RFC 5322 source.
    raw: bytes = field(repr=False)
    folder_ids: tuple[str, ...] = ()
    flags: frozenset[str] = frozenset()
    provider_thread_id: str | None = None
    # Server receive time (IMAP INTERNALDATE, Graph receivedDateTime, Gmail internalDate).
    received_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class MessageFetched:
    message: RawMessage


@dataclass(frozen=True, slots=True)
class MessageDeleted:
    remote_ref: str


@dataclass(frozen=True, slots=True)
class MessageUpdated:
    """Flags or folders/labels of a known message changed."""

    remote_ref: str
    flags: frozenset[str] | None = None
    folder_ids: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class CursorAdvanced:
    cursor: SyncCursor


SyncEvent = MessageFetched | MessageDeleted | MessageUpdated | CursorAdvanced


@dataclass(frozen=True, slots=True)
class ChangeEvent:
    """Push signal: ``folder_id`` (or the whole mailbox if ``None``) has changes."""

    folder_id: str | None = None


class ProviderError(Exception):
    """Base class for provider failures. ``code`` is stored in ``SyncState.last_error``;
    messages must not contain mail data or credentials."""

    code = "provider_error"

    def __init__(self, *args: object, code: str | None = None) -> None:
        super().__init__(*args)
        if code is not None:
            self.code = code


class AuthenticationError(ProviderError):
    code = "authentication_failed"


class ConnectionFailedError(ProviderError):
    """The server could not be reached or the connection broke (DNS, TCP, TLS, timeout).
    Usually transient, so callers retry."""

    code = "connection_failed"


class ConfigurationError(ProviderError):
    """The mailbox settings are invalid or not allowed by the instance settings (e.g. an
    unencrypted connection without the admin flag). Retrying does not help."""

    code = "invalid_configuration"


class CursorInvalidError(ProviderError):
    """The cursor is no longer valid (IMAP UIDVALIDITY changed, Graph delta token
    expired, Gmail history too old); the caller must resync the folder from scratch."""

    code = "cursor_invalid"


class MessageNotFoundError(ProviderError):
    code = "message_not_found"


@runtime_checkable
class MailProvider(Protocol):
    capabilities: ProviderCapabilities

    async def list_folders(self) -> list[RemoteFolder]: ...

    def fetch_since(
        self, folder_id: str, cursor: SyncCursor | None, *, since: datetime | None = None
    ) -> AsyncIterator[SyncEvent]:
        """Yield changes in ``folder_id`` after ``cursor``. Without a cursor this is an
        initial import, limited to messages received after ``since``. The last event is
        always ``CursorAdvanced``."""
        ...

    def watch(self, folder_id: str | None = None) -> AsyncIterator[ChangeEvent]:
        """Yield a ``ChangeEvent`` whenever the server reports changes."""
        ...

    async def move(self, remote_ref: str, target_folder_id: str) -> str:
        """Move a message and return its new ``remote_ref`` (IMAP UIDs change)."""
        ...

    async def set_flags(self, remote_ref: str, flags: frozenset[str]) -> None:
        """Replace the message's flags (``Flag`` values and keywords)."""
        ...

    async def apply_label(self, remote_ref: str, label: str) -> None:
        """Gmail: add a label. Graph: add a category. IMAP: set a keyword, or copy into a
        folder if the server does not support keywords."""
        ...

    async def remove_label(self, remote_ref: str, label: str) -> None: ...

    async def aclose(self) -> None:
        """Release connections."""
        ...
