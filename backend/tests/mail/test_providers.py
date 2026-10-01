import asyncio
from datetime import UTC, datetime

import pytest

from app.mail.models import FolderKind, FolderRole, MailboxType
from app.mail.providers.base import (
    ChangeEvent,
    CursorAdvanced,
    CursorInvalidError,
    MailboxConfig,
    MailProvider,
    MessageDeleted,
    MessageFetched,
    MessageNotFoundError,
    MessageUpdated,
    RemoteFolder,
    SyncCursor,
    SyncEvent,
)
from app.mail.providers.fake import FakeMailProvider
from app.mail.providers.registry import ProviderRegistry, UnknownProviderError
from tests.mail.conftest import load_fixture

CONFIG = MailboxConfig(mailbox_id="m1", type=MailboxType.IMAP, address="erika@example.org")
RAW = load_fixture("01-plain-ascii.eml")


def make_provider(*, labels: bool = False) -> FakeMailProvider:
    provider = FakeMailProvider(CONFIG, labels=labels)
    provider.add_folder(RemoteFolder("INBOX", "Inbox", role=FolderRole.INBOX))
    provider.add_folder(RemoteFolder("Archive", "Archive", role=FolderRole.ARCHIVE))
    return provider


async def collect(
    provider: FakeMailProvider, folder: str, cursor: SyncCursor | None = None
) -> list[SyncEvent]:
    return [event async for event in provider.fetch_since(folder, cursor)]


def cursor_of(events: list[SyncEvent]) -> SyncCursor:
    last = events[-1]
    assert isinstance(last, CursorAdvanced)
    return last.cursor


def test_fake_provider_satisfies_protocol() -> None:
    assert isinstance(make_provider(), MailProvider)


def test_registry_creates_registered_provider() -> None:
    registry = ProviderRegistry()
    registry.register(MailboxType.IMAP, FakeMailProvider)

    provider = registry.create(CONFIG)

    assert isinstance(provider, FakeMailProvider)
    assert provider.config == CONFIG
    assert registry.types == {MailboxType.IMAP}


def test_registry_rejects_unknown_and_duplicate_types() -> None:
    registry = ProviderRegistry()
    registry.register(MailboxType.IMAP, FakeMailProvider)

    with pytest.raises(UnknownProviderError):
        registry.create(MailboxConfig(mailbox_id="m", type=MailboxType.GMAIL, address="a@b.c"))
    with pytest.raises(ValueError, match="already registered"):
        registry.register(MailboxType.IMAP, FakeMailProvider)

    registry.register(MailboxType.IMAP, lambda c: FakeMailProvider(c, labels=True), replace=True)
    assert registry.create(CONFIG).capabilities.labels
    registry.unregister(MailboxType.IMAP)
    assert not registry.is_registered(MailboxType.IMAP)


def test_credentials_are_not_in_repr() -> None:
    config = MailboxConfig("m", MailboxType.IMAP, "a@example.com", credentials={"password": "x1"})

    assert "x1" not in repr(config)


async def test_initial_fetch_then_incremental_changes() -> None:
    provider = make_provider()
    first = provider.add_message("INBOX", RAW)

    initial = await collect(provider, "INBOX")
    assert [type(e) for e in initial] == [MessageFetched, CursorAdvanced]

    second = provider.add_message("INBOX", RAW, flags=frozenset({"seen"}))
    await provider.set_flags(first, frozenset({"flagged"}))
    provider.delete_message(second)

    changes = await collect(provider, "INBOX", cursor_of(initial))
    assert changes[:-1] == [
        MessageDeleted(second),
        MessageUpdated(first, flags=frozenset({"flagged"}), folder_ids=("INBOX",)),
        MessageDeleted(second),
    ]
    # Nothing new after the latest cursor.
    latest = await collect(provider, "INBOX", cursor_of(changes))
    assert latest == [CursorAdvanced(cursor_of(changes))]


async def test_initial_fetch_respects_since() -> None:
    provider = make_provider()
    provider.add_message("INBOX", RAW, received_at=datetime(2025, 1, 1, tzinfo=UTC))
    recent = provider.add_message("INBOX", RAW, received_at=datetime(2026, 9, 1, tzinfo=UTC))

    events = [
        e async for e in provider.fetch_since("INBOX", None, since=datetime(2026, 1, 1, tzinfo=UTC))
    ]

    assert [e.message.remote_ref for e in events if isinstance(e, MessageFetched)] == [recent]


async def test_move_in_folder_mode_changes_reference() -> None:
    provider = make_provider()
    ref = provider.add_message("INBOX", RAW)
    cursor = cursor_of(await collect(provider, "INBOX"))

    new_ref = await provider.move(ref, "Archive")

    assert new_ref != ref
    assert MessageDeleted(ref) in await collect(provider, "INBOX", cursor)
    archived = await collect(provider, "Archive")
    assert [e.message.remote_ref for e in archived if isinstance(e, MessageFetched)] == [new_ref]


async def test_label_mode_behaves_like_gmail() -> None:
    provider = make_provider(labels=True)
    folders = await provider.list_folders()
    assert {f.kind for f in folders} == {FolderKind.LABEL}

    ref = provider.add_message("INBOX", RAW, provider_thread_id="t1")
    archive_cursor = cursor_of(await collect(provider, "Archive"))

    await provider.apply_label(ref, "Archive")
    assert provider.messages[ref].folder_ids == ("INBOX", "Archive")
    assert await provider.move(ref, "Archive") == ref

    changes = await collect(provider, "Archive", archive_cursor)
    assert isinstance(changes[0], MessageUpdated)

    inbox_cursor = cursor_of(await collect(provider, "INBOX"))
    await provider.remove_label(ref, "INBOX")
    assert MessageDeleted(ref) in await collect(provider, "INBOX", inbox_cursor)


async def test_keywords_in_folder_mode() -> None:
    provider = make_provider()
    ref = provider.add_message("INBOX", RAW)

    await provider.apply_label(ref, "ollamail/important")
    assert "ollamail/important" in provider.messages[ref].flags
    await provider.remove_label(ref, "ollamail/important")
    assert provider.messages[ref].flags == frozenset()
    assert [a.name for a in provider.actions] == ["apply_label", "remove_label"]


async def test_invalidated_cursor_raises() -> None:
    provider = make_provider()
    cursor = cursor_of(await collect(provider, "INBOX"))
    provider.invalidate_cursors()

    with pytest.raises(CursorInvalidError) as info:
        await collect(provider, "INBOX", cursor)
    assert info.value.code == "cursor_invalid"


async def test_unknown_message_raises() -> None:
    with pytest.raises(MessageNotFoundError):
        await make_provider().set_flags("missing", frozenset())


async def test_watch_signals_changes() -> None:
    provider = make_provider()
    events: list[ChangeEvent] = []

    async def consume() -> None:
        async for event in provider.watch("INBOX"):
            events.append(event)
            return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    provider.add_message("Archive", RAW)  # not watched
    provider.add_message("INBOX", RAW)
    await asyncio.wait_for(task, timeout=1)

    assert events == [ChangeEvent("INBOX")]


async def test_watch_without_push_raises() -> None:
    provider = FakeMailProvider(push=False)

    with pytest.raises(NotImplementedError):
        await anext(provider.watch())


async def test_aclose() -> None:
    provider = make_provider()
    await provider.aclose()

    assert provider.closed
