"""Contract tests of the Gmail provider against synthetic Gmail API responses (respx)."""

import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.core.config import GmailSettings, MailSettings
from app.mail.models import FolderKind, FolderRole, MailboxType
from app.mail.providers.base import (
    MAILBOX_SCOPE,
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
    ProviderError,
    SyncCursor,
    SyncEvent,
)
from app.mail.providers.gmail import ALL_MAIL, GmailProvider
from app.mail.providers.registry import registry
from tests.mail.gmail_server import GmailServer, StaticTokens, google_error

ADDRESS = "test.user@example.com"
SINCE = datetime(2026, 7, 1, tzinfo=UTC)
# Newer than SINCE (internalDate in milliseconds).
RECENT = int(datetime(2026, 9, 20, tzinfo=UTC).timestamp() * 1000)
OLD = int(datetime(2026, 1, 5, tzinfo=UTC).timestamp() * 1000)


def mail(n: int) -> bytes:
    return (
        f"From: Sender {n} <sender{n}@example.com>\r\n"
        f"To: Test User <{ADDRESS}>\r\n"
        f"Subject: Synthetic message {n}\r\n"
        f"Message-ID: <synthetic-{n}@example.com>\r\n"
        "Date: Sun, 20 Sep 2026 10:00:00 +0000\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
        f"Body of synthetic message {n}.\r\n"
    ).encode()


async def no_sleep(seconds: float) -> None:
    return None


@pytest.fixture
def server() -> Iterator[GmailServer]:
    gmail = GmailServer(address=ADDRESS)
    with respx.mock(assert_all_called=False) as router:
        gmail.install(router)
        yield gmail


@pytest.fixture
def tokens() -> StaticTokens:
    return StaticTokens()


def make_provider(
    tokens: StaticTokens,
    settings: dict[str, Any] | None = None,
    *,
    gmail: GmailSettings | None = None,
    batch_size: int = 50,
    pubsub: StaticTokens | None = None,
) -> GmailProvider:
    config = MailboxConfig(
        mailbox_id="mb-1",
        type=MailboxType.GMAIL,
        address=ADDRESS,
        settings=settings or {},
        credentials={"refresh_token": "test-refresh-token"},
    )
    return GmailProvider(
        config,
        settings=gmail or GmailSettings(),
        mail_settings=MailSettings(sync_batch_size=batch_size),
        token_source=tokens,
        pubsub_token_source=pubsub,
        sleep=no_sleep,
    )


@pytest.fixture
async def provider(server: GmailServer, tokens: StaticTokens) -> AsyncIterator[GmailProvider]:
    gmail = make_provider(tokens)
    yield gmail
    await gmail.aclose()


async def collect(
    provider: GmailProvider, cursor: SyncCursor | None = None, since: datetime | None = SINCE
) -> list[SyncEvent]:
    return [event async for event in provider.fetch_since(MAILBOX_SCOPE, cursor, since=since)]


def last_cursor(events: list[SyncEvent]) -> SyncCursor:
    assert isinstance(events[-1], CursorAdvanced)
    return events[-1].cursor


def fetched(events: list[SyncEvent]) -> list[MessageFetched]:
    return [e for e in events if isinstance(e, MessageFetched)]


# -- folders -------------------------------------------------------------------------------


async def test_labels_map_to_folders(server: GmailServer, provider: GmailProvider) -> None:
    projects = server.add_label("Projects")
    alpha = server.add_label("Projects/Alpha")

    folders = {f.remote_id: f for f in await provider.list_folders()}

    assert set(folders) == {"INBOX", "SENT", "DRAFT", "SPAM", "TRASH", ALL_MAIL, projects, alpha}
    assert all(f.kind is FolderKind.LABEL for f in folders.values())
    assert folders["INBOX"].role is FolderRole.INBOX
    assert folders["DRAFT"].role is FolderRole.DRAFTS
    assert folders["SPAM"].role is FolderRole.JUNK
    assert folders["TRASH"].role is FolderRole.TRASH
    assert folders[ALL_MAIL].role is FolderRole.ALL
    assert (folders[alpha].name, folders[alpha].parent_id) == ("Projects/Alpha", projects)
    assert folders[projects].parent_id is None


def test_capabilities(server: GmailServer, tokens: StaticTokens) -> None:
    provider = make_provider(tokens)
    caps = provider.capabilities
    assert (caps.labels, caps.server_threads, caps.mailbox_cursor) == (True, True, True)
    assert caps.push is False
    assert caps.keywords is False


def test_registered_for_gmail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMAIL_GMAIL_CLIENT_ID", "client")
    config = MailboxConfig(
        mailbox_id="mb",
        type=MailboxType.GMAIL,
        address=ADDRESS,
        credentials={"refresh_token": "rt"},
    )
    gmail = GmailSettings(client_id="client", client_secret="secret")  # type: ignore[arg-type]
    monkeypatch.setattr("app.mail.providers.gmail.get_settings", lambda: _settings(gmail))
    assert isinstance(registry.create(config), GmailProvider)


def _settings(gmail: GmailSettings) -> Any:
    from app.core.config import Settings

    return Settings(gmail=gmail)


# -- initial import --------------------------------------------------------------------------


async def test_initial_import_pages_and_batches(server: GmailServer, tokens: StaticTokens) -> None:
    label = server.add_label("Receipts")
    ids = [
        server.add_message(mail(1), ["INBOX", "UNREAD"], internal_date=RECENT + 1),
        server.add_message(mail(2), ["INBOX", "STARRED"], internal_date=RECENT + 2),
        server.add_message(mail(3), [label], internal_date=RECENT + 3, thread_id="thread-a"),
        server.add_message(mail(4), ["SPAM"], internal_date=RECENT + 4),
        server.add_message(mail(5), ["INBOX"], internal_date=OLD),
        server.add_message(mail(6), ["SENT"], internal_date=RECENT + 6),
    ]
    history_at_start = server.history_id
    provider = make_provider(tokens, batch_size=2)

    events = await collect(provider)

    messages = {e.message.remote_ref: e.message for e in fetched(events)}
    # Spam is skipped (includeSpamTrash=false), the old message is outside the window.
    assert set(messages) == {ids[0], ids[1], ids[2], ids[5]}
    assert all(e.initial for e in fetched(events))
    # Newest first, as messages.list returns them.
    assert [e.message.remote_ref for e in fetched(events)] == [ids[5], ids[2], ids[1], ids[0]]
    first = messages[ids[0]]
    assert first.raw == mail(1)
    assert first.folder_ids == ("INBOX", ALL_MAIL)
    assert first.flags == frozenset()
    assert messages[ids[1]].flags == {"seen", "flagged"}
    assert messages[ids[2]].folder_ids == (label, ALL_MAIL)
    assert messages[ids[2]].provider_thread_id == "thread-a"
    assert first.received_at == datetime.fromtimestamp((RECENT + 1) / 1000, UTC)
    # One CursorAdvanced per page; the import state is resumable.
    cursors = [e.cursor.data for e in events if isinstance(e, CursorAdvanced)]
    assert cursors[0] == {
        "v": 1,
        "history_id": str(history_at_start),
        "import": {"q": f"after:{int(SINCE.timestamp())}", "page_token": "2"},
    }
    assert cursors[-1] == {"v": 1, "history_id": str(history_at_start)}
    assert server.batch_sizes == [2, 2]
    list_request = next(r for r in server.requests if r.url.path.endswith("/messages"))
    assert list_request.url.params["includeSpamTrash"] == "false"


async def test_resumes_import_and_restarts_on_expired_page_token(
    server: GmailServer, provider: GmailProvider
) -> None:
    ref = server.add_message(mail(1), internal_date=RECENT)
    cursor = SyncCursor(
        {
            "v": 1,
            "history_id": str(server.history_id),
            "import": {"q": f"after:{int(SINCE.timestamp())}", "page_token": "expired"},
        }
    )

    events = await collect(provider, cursor)

    assert [e.message.remote_ref for e in fetched(events)] == [ref]
    assert "import" not in last_cursor(events).data


async def test_chat_messages_are_not_imported(server: GmailServer, provider: GmailProvider) -> None:
    server.add_message(mail(1), ["CHAT"], internal_date=RECENT)
    events = await collect(provider)
    assert fetched(events) == []


# -- incremental sync ----------------------------------------------------------------------


async def synced(server: GmailServer, provider: GmailProvider) -> SyncCursor:
    return last_cursor(await collect(provider))


async def test_history_reports_changes(server: GmailServer, provider: GmailProvider) -> None:
    keep = server.add_message(mail(1), ["INBOX", "UNREAD"], internal_date=RECENT)
    gone = server.add_message(mail(2), ["INBOX"], internal_date=RECENT)
    trashed = server.add_message(mail(3), ["INBOX"], internal_date=RECENT)
    cursor = await synced(server, provider)

    new = server.add_message(mail(4), ["INBOX", "UNREAD"], internal_date=RECENT + 10)
    server.delete_message(gone)
    server.change_labels(keep, add=["STARRED"], remove=["UNREAD", "INBOX"])
    server.change_labels(trashed, add=["TRASH"], remove=["INBOX"])

    events = await collect(provider, cursor)

    assert MessageDeleted(gone) in events
    assert MessageDeleted(trashed) in events
    update = next(e for e in events if isinstance(e, MessageUpdated))
    assert update == MessageUpdated(
        keep, flags=frozenset({"seen", "flagged"}), folder_ids=(ALL_MAIL,)
    )
    assert [(e.message.remote_ref, e.initial) for e in fetched(events)] == [(new, False)]
    assert last_cursor(events).data == {"v": 1, "history_id": str(server.history_id)}
    history = next(r for r in server.requests if r.url.path.endswith("/history"))
    assert history.url.params.get_list("historyTypes") == [
        "messageAdded",
        "messageDeleted",
        "labelAdded",
        "labelRemoved",
    ]


async def test_no_changes_costs_one_history_call(
    server: GmailServer, provider: GmailProvider
) -> None:
    server.add_message(mail(1), internal_date=RECENT)
    cursor = await synced(server, provider)
    server.requests.clear()

    events = await collect(provider, cursor)

    assert events == [CursorAdvanced(cursor)]
    assert server.paths() == ["/gmail/v1/users/me/history"]


async def test_message_restored_from_trash_is_fetched(
    server: GmailServer, provider: GmailProvider
) -> None:
    ref = server.add_message(mail(1), ["TRASH"], internal_date=RECENT)
    cursor = await synced(server, provider)

    server.change_labels(ref, add=["INBOX"], remove=["TRASH"])
    events = await collect(provider, cursor)

    assert [e.message.remote_ref for e in fetched(events)] == [ref]


async def test_added_then_vanished_message_is_deleted(
    server: GmailServer, provider: GmailProvider
) -> None:
    cursor = await synced(server, provider)
    ref = server.add_message(mail(1), internal_date=RECENT)
    # Deleted after the history page was read but before the source was fetched.
    server.fail("GET", f"/messages/{ref}$", google_error(404, "notFound"))

    events = await collect(provider, cursor)

    assert MessageDeleted(ref) in events
    assert fetched(events) == []


async def test_history_pages_advance_the_cursor(server: GmailServer, tokens: StaticTokens) -> None:
    provider = make_provider(tokens)
    cursor = await synced(server, provider)
    for n in range(3):
        server.add_message(mail(n), internal_date=RECENT + n)
    import app.mail.providers.gmail as gmail_module

    original = gmail_module.HISTORY_PAGE
    gmail_module.HISTORY_PAGE = 2
    try:
        events = await collect(provider, cursor)
    finally:
        gmail_module.HISTORY_PAGE = original

    cursors = [e.cursor.data["history_id"] for e in events if isinstance(e, CursorAdvanced)]
    assert cursors == [str(server.history_id - 1), str(server.history_id)]
    assert len(fetched(events)) == 3


async def test_expired_history_requires_resync(
    server: GmailServer, provider: GmailProvider
) -> None:
    cursor = await synced(server, provider)
    server.add_message(mail(1), internal_date=RECENT)
    server.expire_history()

    with pytest.raises(CursorInvalidError):
        await collect(provider, cursor)


@pytest.mark.parametrize(
    "data",
    [{}, {"v": 2, "history_id": "1"}, {"v": 1, "history_id": "abc"}, {"v": 1, "import": 3}],
)
async def test_malformed_cursor_is_invalid(provider: GmailProvider, data: dict[str, Any]) -> None:
    with pytest.raises(CursorInvalidError):
        await collect(provider, SyncCursor(data))


async def test_only_mailbox_scope_is_supported(provider: GmailProvider) -> None:
    with pytest.raises(ProviderError) as info:
        await anext(provider.fetch_since("INBOX", None))
    assert info.value.code == "folder_scope_unsupported"


async def test_include_spam_trash(server: GmailServer, tokens: StaticTokens) -> None:
    ref = server.add_message(mail(1), ["SPAM"], internal_date=RECENT)
    provider = make_provider(tokens, {"include_spam_trash": True})

    events = await collect(provider)

    assert [(e.message.remote_ref, e.message.folder_ids) for e in fetched(events)] == [
        (ref, ("SPAM",))
    ]


# -- errors, retries, tokens --------------------------------------------------------------------


async def test_rate_limits_are_retried(server: GmailServer, tokens: StaticTokens) -> None:
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    provider = make_provider(tokens)
    provider._api._sleep = sleep
    ref = server.add_message(mail(1), internal_date=RECENT)
    server.fail("GET", "/messages$", httpx.Response(429, headers={"Retry-After": "3"}))
    server.fail("GET", "/profile$", google_error(403, "userRateLimitExceeded"))
    server.fail("GET", f"/messages/{ref}$", google_error(503, "backendError"))

    events = await collect(provider)

    assert [e.message.remote_ref for e in fetched(events)] == [ref]
    assert len(sleeps) == 3
    assert 3.0 in sleeps


async def test_persistent_rate_limit_fails_as_connection_error(
    server: GmailServer, provider: GmailProvider
) -> None:
    server.fail("GET", "/profile$", google_error(429, "rateLimitExceeded"), times=10)

    with pytest.raises(ConnectionFailedError) as info:
        await collect(provider)
    assert info.value.code == "rate_limited"


async def test_unauthorized_refreshes_token_once(
    server: GmailServer, provider: GmailProvider, tokens: StaticTokens
) -> None:
    server.fail("GET", "/labels$", httpx.Response(401))
    await provider.list_folders()
    assert tokens.refreshes == 1
    auth = [r.headers["Authorization"] for r in server.requests]
    assert auth == ["Bearer test-access-token-0", "Bearer test-access-token-1"]

    server.fail("GET", "/labels$", httpx.Response(401), times=2)
    with pytest.raises(AuthenticationError):
        await provider.list_folders()


async def test_insufficient_scope(server: GmailServer, provider: GmailProvider) -> None:
    server.fail("GET", "/labels$", google_error(403, "insufficientPermissions"))
    with pytest.raises(AuthenticationError) as info:
        await provider.list_folders()
    assert info.value.code == "insufficient_scope"
    # Google's error text never ends up in the exception.
    assert "synthetic" not in repr(info.value)


async def test_api_disabled_is_a_configuration_error(
    server: GmailServer, provider: GmailProvider
) -> None:
    server.fail("GET", "/labels$", google_error(403, "accessNotConfigured"))
    with pytest.raises(ConfigurationError) as info:
        await provider.list_folders()
    assert info.value.code == "api_disabled"


async def test_network_errors(server: GmailServer, provider: GmailProvider) -> None:
    server.fail("GET", "/labels$", httpx.ConnectError("synthetic"), times=10)
    with pytest.raises(ConnectionFailedError):
        await provider.list_folders()


def test_missing_refresh_token(tokens: StaticTokens) -> None:
    config = MailboxConfig(mailbox_id="mb", type=MailboxType.GMAIL, address=ADDRESS)
    gmail = GmailSettings(client_id="c", client_secret="s")  # type: ignore[arg-type]
    with pytest.raises(AuthenticationError) as info:
        GmailProvider(config, settings=gmail)
    assert info.value.code == "credentials_missing"


def test_oauth_client_not_configured() -> None:
    config = MailboxConfig(
        mailbox_id="mb", type=MailboxType.GMAIL, address=ADDRESS, credentials={"refresh_token": "x"}
    )
    with pytest.raises(ConfigurationError) as info:
        GmailProvider(config, settings=GmailSettings())
    assert info.value.code == "oauth_not_configured"


@pytest.mark.parametrize(
    "settings",
    [{"auth": "imap"}, {"pubsub_topic": "not-a-topic"}, {"include_spam_trash": "maybe"}],
)
def test_invalid_settings(settings: dict[str, Any]) -> None:
    config = MailboxConfig(
        mailbox_id="mb", type=MailboxType.GMAIL, address=ADDRESS, settings=settings
    )
    with pytest.raises(ConfigurationError):
        GmailProvider(config, settings=GmailSettings())


def test_service_account_needs_key_file(tmp_path: Path) -> None:
    config = MailboxConfig(
        mailbox_id="mb",
        type=MailboxType.GMAIL,
        address=ADDRESS,
        settings={"auth": "service_account"},
    )
    with pytest.raises(ConfigurationError) as info:
        GmailProvider(config, settings=GmailSettings())
    assert info.value.code == "service_account_missing"
    with pytest.raises(ConfigurationError) as info:
        GmailProvider(
            config, settings=GmailSettings(service_account_file=tmp_path / "missing.json")
        )
    assert info.value.code == "service_account_invalid"


# -- actions --------------------------------------------------------------------------------


async def test_set_flags(server: GmailServer, provider: GmailProvider) -> None:
    ref = server.add_message(mail(1), ["INBOX", "UNREAD"])

    await provider.set_flags(ref, frozenset({"seen", "flagged"}))
    assert set(server.messages[ref].label_ids) == {"INBOX", "STARRED"}

    await provider.set_flags(ref, frozenset())
    assert set(server.messages[ref].label_ids) == {"INBOX", "UNREAD"}


async def test_archive_and_trash(server: GmailServer, provider: GmailProvider) -> None:
    ref = server.add_message(mail(1), ["INBOX"])

    assert await provider.move(ref, ALL_MAIL) == ref
    assert server.messages[ref].label_ids == []

    await provider.archive(ref)
    assert await provider.move(ref, "TRASH") == ref
    assert server.modifications[-1] == (ref, {"trash": True})
    assert server.messages[ref].label_ids == ["TRASH"]


async def test_move_to_label(server: GmailServer, provider: GmailProvider) -> None:
    label = server.add_label("Receipts")
    ref = server.add_message(mail(1), ["INBOX", "UNREAD"])

    await provider.move(ref, label)

    assert set(server.messages[ref].label_ids) == {"UNREAD", label}
    with pytest.raises(ProviderError) as info:
        await provider.move(ref, "Label_404")
    assert info.value.code == "folder_not_found"


async def test_apply_and_remove_label(server: GmailServer, provider: GmailProvider) -> None:
    existing = server.add_label("Follow-up")
    ref = server.add_message(mail(1), ["INBOX"])

    await provider.apply_label(ref, "follow-up")
    assert existing in server.messages[ref].label_ids

    await provider.apply_label(ref, "Waiting")
    created = next(i for i, label in server.labels.items() if label["name"] == "Waiting")
    assert created in server.messages[ref].label_ids

    await provider.remove_label(ref, "Waiting")
    await provider.remove_label(ref, "Unknown label")
    assert set(server.messages[ref].label_ids) == {"INBOX", existing}
    with pytest.raises(ProviderError):
        await provider.apply_label(ref, ALL_MAIL)


async def test_apply_label_created_concurrently(
    server: GmailServer, provider: GmailProvider
) -> None:
    ref = server.add_message(mail(1), ["INBOX"])
    await provider.list_folders()
    # Created by someone else after our label list was loaded.
    label = server.add_label("Late")

    await provider.apply_label(ref, "Late")

    assert label in server.messages[ref].label_ids


async def test_action_on_missing_message(server: GmailServer, provider: GmailProvider) -> None:
    with pytest.raises(MessageNotFoundError):
        await provider.set_flags("18f0000000000404", frozenset())
    with pytest.raises(MessageNotFoundError):
        await provider.move("18f0000000000404", "TRASH")


async def test_readonly_refuses_actions(server: GmailServer, tokens: StaticTokens) -> None:
    ref = server.add_message(mail(1), ["INBOX"])
    provider = make_provider(tokens, gmail=GmailSettings(readonly=True))
    for action in (
        provider.set_flags(ref, frozenset()),
        provider.move(ref, ALL_MAIL),
        provider.apply_label(ref, "x"),
        provider.remove_label(ref, "INBOX"),
    ):
        with pytest.raises(ProviderError) as info:
            await action
        assert info.value.code == "read_only"
    assert server.modifications == []


# -- push -----------------------------------------------------------------------------------

PUBSUB_SETTINGS = {
    "pubsub_topic": "projects/test-project/topics/gmail",
    "pubsub_subscription": "projects/test-project/subscriptions/gmail-test-user",
}


async def test_without_pubsub_there_is_no_push(provider: GmailProvider) -> None:
    with pytest.raises(NotImplementedError):
        await anext(provider.watch())


async def test_pubsub_pull_notifications(server: GmailServer, tokens: StaticTokens) -> None:
    pubsub = StaticTokens()
    provider = make_provider(tokens, PUBSUB_SETTINGS, pubsub=pubsub)
    assert provider.capabilities.push is True
    server.notify(ack_id="mine")
    server.notify(address="other.user@example.com", ack_id="theirs")

    watch = provider.watch()
    assert await anext(watch) == ChangeEvent(None)
    await watch.aclose()

    assert server.watch_calls == [{"topicName": PUBSUB_SETTINGS["pubsub_topic"]}]
    assert (server.acked, server.nacked) == (["mine"], ["theirs"])
    pull = next(r for r in server.requests if r.url.path.endswith(":pull"))
    assert pull.url.path == f"/v1/{PUBSUB_SETTINGS['pubsub_subscription']}:pull"
    assert pull.headers["Authorization"] == "Bearer test-access-token-0"
    assert json.loads(pull.content) == {"maxMessages": 10}


def test_pubsub_without_service_account_falls_back_to_polling(tokens: StaticTokens) -> None:
    provider = make_provider(tokens, PUBSUB_SETTINGS)
    assert provider.capabilities.push is False
