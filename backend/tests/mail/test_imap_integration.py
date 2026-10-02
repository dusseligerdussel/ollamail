"""``ImapProvider`` against a real IMAP server (Dovecot, see ``imap_server``).

Dovecot supports CONDSTORE, QRESYNC, ESEARCH, MOVE and UIDPLUS. The fallback paths for
servers without them are covered by disabling the extensions on the client side.
"""

import asyncio
import dataclasses
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.mail.models import FolderRole
from app.mail.providers.base import (
    AuthenticationError,
    ConnectionFailedError,
    CursorAdvanced,
    CursorInvalidError,
    MessageDeleted,
    MessageFetched,
    MessageNotFoundError,
    MessageUpdated,
    SyncCursor,
    SyncEvent,
)
from app.mail.providers.imap import parse_ref
from app.mail.providers.imap_client import ImapConnection, SelectInfo
from tests.mail.imap_server import TestAccount, message

pytestmark = pytest.mark.imap

# Extension sets the client ignores: all available, CONDSTORE only, plain IMAP4rev1.
MODES = {
    "qresync": frozenset[str](),
    "condstore": frozenset({"QRESYNC"}),
    "plain": frozenset({"QRESYNC", "CONDSTORE", "ESEARCH", "MOVE", "SPECIAL-USE"}),
}


@pytest.fixture(params=sorted(MODES))
def disabled(request: pytest.FixtureRequest) -> frozenset[str]:
    return MODES[request.param]


async def collect(events: AsyncIterator[SyncEvent]) -> list[SyncEvent]:
    return [event async for event in events]


def fetched_uids(events: list[SyncEvent]) -> list[int]:
    return [parse_ref(e.message.remote_ref)[2] for e in events if isinstance(e, MessageFetched)]


def last_cursor(events: list[SyncEvent]) -> SyncCursor:
    cursors = [e.cursor for e in events if isinstance(e, CursorAdvanced)]
    assert cursors, "fetch_since must end with CursorAdvanced"
    assert isinstance(events[-1], CursorAdvanced)
    return cursors[-1]


async def test_tls_certificate_is_verified_by_default(imap_account: TestAccount) -> None:
    provider = imap_account.provider(settings={"verify_certificate": True})
    with pytest.raises(ConnectionFailedError) as error:
        await provider.verify()
    assert error.value.code == "tls_certificate_invalid"


async def test_starttls_login_and_wrong_password(imap_account: TestAccount) -> None:
    from tests.mail.imap_server import STARTTLS_PORT

    provider = imap_account.provider(settings={"security": "starttls", "port": STARTTLS_PORT})
    await provider.verify()
    await provider.aclose()

    wrong = imap_account.provider(settings={"password": "not-the-password"})
    with pytest.raises(AuthenticationError):
        await wrong.verify()


async def test_list_folders_with_special_use_and_hierarchy(imap_account: TestAccount) -> None:
    await imap_account.run("CREATE", "Projekte")
    await imap_account.run("CREATE", "Projekte/Entw&APw-rfe")
    provider = imap_account.provider()
    try:
        folders = {f.remote_id: f for f in await provider.list_folders()}
    finally:
        await provider.aclose()

    assert folders["INBOX"].role is FolderRole.INBOX
    assert folders["Sent"].role is FolderRole.SENT
    assert folders["Trash"].role is FolderRole.TRASH
    assert folders["Junk"].role is FolderRole.JUNK
    assert folders["Drafts"].role is FolderRole.DRAFTS
    nested = folders["Projekte/Entw&APw-rfe"]
    assert (nested.name, nested.parent_id, nested.role) == ("Entwürfe", "Projekte", None)


async def test_initial_import_newest_first_in_batches(
    imap_account: TestAccount, disabled: frozenset[str]
) -> None:
    now = datetime.now(UTC)
    for number in range(1, 8):
        received = now - timedelta(days=200 if number in (1, 2) else 1)
        await imap_account.append(message(number), received=received, flags=frozenset({"seen"}))
    provider = imap_account.provider(batch_size=2, disabled_extensions=disabled)
    try:
        events = await collect(provider.fetch_since("INBOX", None, since=now - timedelta(days=90)))
    finally:
        await provider.aclose()

    # Messages 1 and 2 are older than the import window.
    assert fetched_uids(events) == [7, 6, 5, 4, 3]
    kinds = [type(e).__name__ for e in events]
    assert kinds == [
        "MessageFetched",
        "MessageFetched",
        "CursorAdvanced",
        "MessageFetched",
        "MessageFetched",
        "CursorAdvanced",
        "MessageFetched",
        "CursorAdvanced",
        "CursorAdvanced",
    ]
    assert all(e.initial for e in events if isinstance(e, MessageFetched))
    first = next(e.message for e in events if isinstance(e, MessageFetched))
    assert first.folder_ids == ("INBOX",)
    assert first.flags == {"seen"}
    assert first.received_at is not None and abs(first.received_at - (now - timedelta(days=1))) < (
        timedelta(seconds=2)
    )
    assert b"Test message 7" in first.raw
    assert last_cursor(events).data["known"] == "3:7"
    assert "import" not in last_cursor(events).data


async def test_interrupted_import_resumes(imap_account: TestAccount) -> None:
    for number in range(1, 6):
        await imap_account.append(message(number))
    provider = imap_account.provider(batch_size=2)
    cursor = None
    seen: list[int] = []
    try:
        async for event in provider.fetch_since("INBOX", None):
            if isinstance(event, MessageFetched):
                seen.append(parse_ref(event.message.remote_ref)[2])
            if isinstance(event, CursorAdvanced):
                cursor = event.cursor
                break  # the job dies after the first batch was committed
    finally:
        await provider.aclose()
    assert seen == [5, 4]
    assert cursor is not None and cursor.data["import"]["below"] == 4

    await imap_account.append(message(6))
    provider = imap_account.provider(batch_size=2)
    try:
        events = await collect(provider.fetch_since("INBOX", cursor))
    finally:
        await provider.aclose()
    # New message first, then the rest of the import; nothing twice.
    assert fetched_uids(events) == [6, 3, 2, 1]
    assert [e.initial for e in events if isinstance(e, MessageFetched)] == [
        False,
        True,
        True,
        True,
    ]
    assert last_cursor(events).data["known"] == "1:6"


async def test_incremental_sync(imap_account: TestAccount, disabled: frozenset[str]) -> None:
    for number in range(1, 5):
        await imap_account.append(message(number))
    provider = imap_account.provider(disabled_extensions=disabled)
    try:
        cursor = last_cursor(await collect(provider.fetch_since("INBOX", None)))

        unchanged = await collect(provider.fetch_since("INBOX", cursor))
        assert not [e for e in unchanged if isinstance(e, MessageFetched | MessageDeleted)]

        await imap_account.run("UID STORE", "2", "+FLAGS", "(\\Seen \\Flagged)", folder="INBOX")
        await imap_account.run("UID STORE", "3", "+FLAGS", "(\\Deleted)", folder="INBOX")
        await imap_account.run("EXPUNGE")
        await imap_account.append(message(5))

        events = await collect(provider.fetch_since("INBOX", cursor))
    finally:
        await provider.aclose()

    uidvalidity = cursor.data["uidvalidity"]
    deleted = [e.remote_ref for e in events if isinstance(e, MessageDeleted)]
    assert deleted == [f"{uidvalidity}:3:INBOX"]
    updates = {e.remote_ref: e.flags for e in events if isinstance(e, MessageUpdated)}
    assert updates[f"{uidvalidity}:2:INBOX"] == frozenset({"seen", "flagged"})
    if not disabled:
        # With CONDSTORE only changed messages are reported.
        assert set(updates) == {f"{uidvalidity}:2:INBOX"}
    assert fetched_uids(events) == [5]
    assert last_cursor(events).data["known"] == "1:2,4:5"


async def test_new_uidvalidity_invalidates_cursor(imap_account: TestAccount) -> None:
    await imap_account.run("CREATE", "Projekte")
    await imap_account.append(message(1), "Projekte")
    provider = imap_account.provider()
    try:
        cursor = last_cursor(await collect(provider.fetch_since("Projekte", None)))
        await imap_account.run("DELETE", "Projekte")
        await imap_account.run("CREATE", "Projekte")
        await imap_account.append(message(2), "Projekte")
        with pytest.raises(CursorInvalidError):
            await collect(provider.fetch_since("Projekte", cursor))
    finally:
        await provider.aclose()


async def test_actions(imap_account: TestAccount, disabled: frozenset[str]) -> None:
    await imap_account.append(message(1))
    await imap_account.run("CREATE", "Archiv")
    provider = imap_account.provider(disabled_extensions=disabled)
    try:
        events = await collect(provider.fetch_since("INBOX", None))
        ref = next(e.message.remote_ref for e in events if isinstance(e, MessageFetched))

        await provider.set_flags(ref, frozenset({"seen", "Warten auf"}))
        await provider.apply_label(ref, "Wichtig")
        await provider.remove_label(ref, "Warten auf")
        changes = await collect(provider.fetch_since("INBOX", last_cursor(events)))
        flags = [e.flags for e in changes if isinstance(e, MessageUpdated) and e.remote_ref == ref]
        assert flags == [frozenset({"seen", "Wichtig"})]

        new_ref = await provider.move(ref, "Archiv")
        folder, _, _ = parse_ref(new_ref)
        assert folder == "Archiv"
        moved = await collect(provider.fetch_since("Archiv", None))
        assert [e.message.remote_ref for e in moved if isinstance(e, MessageFetched)] == [new_ref]
        assert next(e.message.flags for e in moved if isinstance(e, MessageFetched)) == {
            "seen",
            "Wichtig",
        }

        with pytest.raises(MessageNotFoundError):
            await provider.set_flags(ref, frozenset())
        with pytest.raises(MessageNotFoundError):
            await provider.move("not-a-ref", "Archiv")
    finally:
        await provider.aclose()


async def test_labels_fall_back_to_folders_without_keyword_support(
    imap_account: TestAccount, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = ImapConnection.select

    async def select_without_keywords(self: ImapConnection, *args: Any, **kwargs: Any) -> Any:
        info: SelectInfo = await original(self, *args, **kwargs)
        return dataclasses.replace(info, permanent_flags=frozenset({"\\SEEN"}))

    monkeypatch.setattr(ImapConnection, "select", select_without_keywords)
    await imap_account.append(message(1))
    provider = imap_account.provider()
    try:
        events = await collect(provider.fetch_since("INBOX", None))
        ref = next(e.message.remote_ref for e in events if isinstance(e, MessageFetched))
        await provider.apply_label(ref, "Wichtig")
        await provider.apply_label(ref, "Wichtig")  # folder exists already
        label_folder = await collect(provider.fetch_since("Wichtig", None))
        assert len(fetched_uids(label_folder)) == 2

        await provider.remove_label(ref, "Wichtig")
        assert fetched_uids(await collect(provider.fetch_since("Wichtig", None))) == []
    finally:
        await provider.aclose()


async def test_idle_reports_new_mail_within_seconds(imap_account: TestAccount) -> None:
    provider = imap_account.provider()
    events = provider.watch()
    waiter = asyncio.ensure_future(anext(events))
    try:
        await asyncio.sleep(1)  # let IDLE start
        started = time.monotonic()
        await imap_account.append(message(1))
        event = await asyncio.wait_for(waiter, timeout=10)
        assert event.folder_id == "INBOX"
        assert time.monotonic() - started < 10
    finally:
        waiter.cancel()
        await events.aclose()  # type: ignore[attr-defined]
        await provider.aclose()


async def test_watch_without_idle_support_raises(imap_account: TestAccount) -> None:
    provider = imap_account.provider(disabled_extensions={"IDLE"})
    with pytest.raises(NotImplementedError):
        await anext(provider.watch())
