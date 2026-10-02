"""Contract tests of ``GraphProvider`` against synthetic Microsoft Graph responses."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import respx

from app.mail.models import FolderRole
from app.mail.providers.base import (
    AuthenticationError,
    ChangeEvent,
    ConfigurationError,
    ConnectionFailedError,
    CursorAdvanced,
    CursorInvalidError,
    Flag,
    MessageChanged,
    MessageDeleted,
    MessageFetched,
    MessageNotFoundError,
    MessageUpdated,
    ProviderError,
    RemoteFolder,
    SyncCursor,
    SyncEvent,
)
from app.mail.providers.graph import GraphProvider
from app.mail.providers.graph_auth import clear_app_token_cache
from app.mail.providers.graph_webhook import verify_client_state
from app.mail.providers.registry import registry
from tests.mail.graph_helpers import (
    ARCHIVE,
    GRAPH,
    GRAPH_HOST,
    INBOX,
    NOW,
    SECURITY,
    TOKEN_URL,
    Sleeps,
    batch_handler,
    credentials,
    delta_url,
    form,
    graph_error,
    graph_settings,
    make_provider,
    message,
    mime,
    mime_part,
    page,
    removed,
    token_response,
)


@pytest.fixture
def graph() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
async def provider() -> AsyncIterator[GraphProvider]:
    instance = make_provider()
    yield instance
    await instance.aclose()


@pytest.fixture(autouse=True)
def _app_tokens() -> Iterator[None]:
    clear_app_token_cache()
    yield
    clear_app_token_cache()


async def collect(events: AsyncIterator[SyncEvent]) -> list[SyncEvent]:
    return [event async for event in events]


def path(suffix: str) -> str:
    return f"/v1.0{suffix}"


# -- folders --------------------------------------------------------------------------------


async def test_folders_with_hierarchy_and_well_known_roles(
    graph: respx.MockRouter, provider: GraphProvider
) -> None:
    roles = {"inbox": INBOX, "archive": ARCHIVE, "sentitems": "sent-id"}

    def well_known(item: dict[str, Any]) -> dict[str, Any]:
        name = item["id"]
        assert item["url"] == f"/me/mailFolders/{name}?$select=id"
        if name in roles:
            return {"id": name, "status": 200, "headers": {}, "body": {"id": roles[name]}}
        return {"id": name, "status": 404, "headers": {}, "body": {"error": {"code": "x"}}}

    graph.post(f"{GRAPH}/$batch").mock(side_effect=batch_handler(well_known))

    def top_level(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("$skip") == "2":
            return httpx.Response(
                200,
                json={"value": [{"id": ARCHIVE, "displayName": "Archiv", "childFolderCount": 0}]},
            )
        assert request.url.params["$select"] == "id,displayName,parentFolderId,childFolderCount"
        return httpx.Response(
            200,
            json={
                "value": [
                    {"id": INBOX, "displayName": "Posteingang", "childFolderCount": 1},
                    {"id": "sent-id", "displayName": "Gesendete Elemente", "childFolderCount": 0},
                ],
                "@odata.nextLink": f"{GRAPH}/me/mailFolders?$skip=2",
            },
        )

    graph.get(host=GRAPH_HOST, path=path("/me/mailFolders")).mock(side_effect=top_level)
    graph.get(host=GRAPH_HOST, path=path(f"/me/mailFolders/{INBOX}/childFolders")).respond(
        json={"value": [{"id": "projects-id", "displayName": "Projekte", "childFolderCount": 0}]}
    )

    folders = await provider.list_folders()

    assert folders == [
        RemoteFolder(INBOX, "Posteingang", role=FolderRole.INBOX),
        RemoteFolder("projects-id", "Posteingang/Projekte", parent_id=INBOX),
        RemoteFolder("sent-id", "Gesendete Elemente", role=FolderRole.SENT),
        RemoteFolder(ARCHIVE, "Archiv", role=FolderRole.ARCHIVE),
    ]


async def test_access_denied_on_folders_is_an_authentication_error(
    graph: respx.MockRouter, provider: GraphProvider
) -> None:
    graph.post(f"{GRAPH}/$batch").mock(
        side_effect=batch_handler(
            lambda item: {"id": item["id"], "status": 403, "headers": {}, "body": {}}
        )
    )
    with pytest.raises(AuthenticationError) as info:
        await provider.list_folders()
    assert info.value.code == "access_denied"


# -- initial import -------------------------------------------------------------------------


async def test_initial_import_pages_through_delta_and_batches_mime(
    graph: respx.MockRouter, provider: GraphProvider
) -> None:
    from datetime import UTC, datetime

    next_link = delta_url(INBOX, "page-2", kind="skiptoken")
    final = delta_url(INBOX, "delta-1")
    delta_requests: list[httpx.Request] = []

    def delta(request: httpx.Request) -> httpx.Response:
        delta_requests.append(request)
        if "$skiptoken" in str(request.url):
            return page([message("m3"), removed("gone")], delta=final)
        return page(
            [message("m1", read=True), message("m2", attachments=True)], next_link=next_link
        )

    graph.get(host=GRAPH_HOST, path=path(f"/me/mailFolders/{INBOX}/messages/delta")).mock(
        side_effect=delta
    )
    batched: list[str] = []

    def mime_batch(item: dict[str, Any]) -> dict[str, Any]:
        batched.append(item["url"])
        assert item["headers"]["Prefer"] == 'IdType="ImmutableId"'
        return mime_part(item["id"], mime())

    graph.post(f"{GRAPH}/$batch").mock(side_effect=batch_handler(mime_batch))
    graph.get(host=GRAPH_HOST, path=path("/me/messages/m2/$value")).respond(
        content=mime("05-nested-multipart.eml"), headers={"Content-Type": "text/plain"}
    )

    since = datetime(2026, 7, 3, 12, 0, tzinfo=UTC)
    events = await collect(provider.fetch_since(INBOX, None, since=since))

    assert [type(e) for e in events] == [
        MessageFetched,
        MessageFetched,
        CursorAdvanced,
        MessageFetched,
        CursorAdvanced,
    ]
    first, second, cursor_1, third, cursor_2 = events
    assert isinstance(first, MessageFetched) and isinstance(second, MessageFetched)
    assert first.initial and first.message.remote_ref == "m1"
    assert first.message.raw == mime()
    assert first.message.flags == {Flag.SEEN}
    assert first.message.folder_ids == (INBOX,)
    assert first.message.provider_thread_id == "conv-m1"
    assert first.message.received_at == datetime(2026, 9, 30, 8, 15, tzinfo=UTC)
    assert second.message.raw == mime("05-nested-multipart.eml")
    assert cursor_1 == CursorAdvanced(SyncCursor({"v": 1, "link": next_link, "initial": True}))
    assert isinstance(third, MessageFetched) and third.initial
    assert cursor_2 == CursorAdvanced(SyncCursor({"v": 1, "link": final, "initial": False}))
    # Only messages without attachments are batched.
    assert batched == ["/me/messages/m1/$value", "/me/messages/m3/$value"]

    first_request = delta_requests[0]
    assert first_request.url.params["$filter"] == "receivedDateTime ge 2026-07-03T12:00:00Z"
    assert "parentFolderId" in first_request.url.params["$select"]
    assert first_request.headers["Authorization"] == "Bearer access-1"
    prefer = first_request.headers["Prefer"]
    assert 'IdType="ImmutableId"' in prefer and "odata.maxpagesize=50" in prefer


async def test_interrupted_import_resumes_at_the_stored_page(
    graph: respx.MockRouter, provider: GraphProvider
) -> None:
    next_link = delta_url(INBOX, "page-7", kind="skiptoken")
    route = graph.get(next_link).mock(return_value=page([], delta=delta_url(INBOX, "d")))

    events = await collect(
        provider.fetch_since(INBOX, SyncCursor({"v": 1, "link": next_link, "initial": True}))
    )

    assert route.called
    assert events == [
        CursorAdvanced(SyncCursor({"v": 1, "link": delta_url(INBOX, "d"), "initial": False}))
    ]


async def test_messages_deleted_during_the_import_are_skipped(
    graph: respx.MockRouter, provider: GraphProvider
) -> None:
    graph.get(host=GRAPH_HOST, path=path(f"/me/mailFolders/{INBOX}/messages/delta")).mock(
        return_value=page(
            [message("m1"), message("m2"), message("m3", attachments=True)],
            delta=delta_url(INBOX, "d"),
        )
    )
    graph.post(f"{GRAPH}/$batch").mock(
        side_effect=batch_handler(
            lambda item: (
                mime_part(item["id"], mime())
                if item["url"].endswith("/m1/$value")
                else {"id": item["id"], "status": 404, "headers": {}, "body": {}}
            )
        )
    )
    graph.get(host=GRAPH_HOST, path=path("/me/messages/m3/$value")).mock(
        return_value=graph_error(404, "ErrorItemNotFound")
    )

    events = await collect(provider.fetch_since(INBOX, None))

    fetched = [e.message.remote_ref for e in events if isinstance(e, MessageFetched)]
    assert fetched == ["m1"]


# -- incremental sync -----------------------------------------------------------------------


async def test_incremental_changes_moves_and_deletions(
    graph: respx.MockRouter, provider: GraphProvider
) -> None:
    link = delta_url(INBOX, "delta-1")
    graph.get(link).mock(
        return_value=page(
            [
                message("c1", read=True, flagged=True, categories=["Projekt A"]),
                removed("moved"),
                removed("deleted"),
            ],
            delta=delta_url(INBOX, "delta-2"),
        )
    )
    graph.get(host=GRAPH_HOST, path=path("/me/messages/moved")).respond(
        json=message("moved", ARCHIVE, read=True)
    )
    graph.get(host=GRAPH_HOST, path=path("/me/messages/deleted")).mock(
        return_value=graph_error(404, "ErrorItemNotFound")
    )
    source = graph.get(host=GRAPH_HOST, path=path("/me/messages/c1/$value")).respond(content=mime())

    events = await collect(
        provider.fetch_since(INBOX, SyncCursor({"v": 1, "link": link, "initial": False}))
    )

    changed, moved, deleted, cursor = events
    assert isinstance(changed, MessageChanged)
    assert changed.remote_ref == "c1"
    assert changed.flags == {Flag.SEEN, Flag.FLAGGED, "Projekt A"}
    assert changed.folder_ids == (INBOX,)
    # The source is only downloaded when the engine asks for it.
    assert not source.called
    raw = await changed.load()
    assert source.called and raw.raw == mime() and raw.remote_ref == "c1"
    assert moved == MessageUpdated("moved", flags=frozenset({Flag.SEEN}), folder_ids=(ARCHIVE,))
    assert deleted == MessageDeleted("deleted")
    assert cursor == CursorAdvanced(
        SyncCursor({"v": 1, "link": delta_url(INBOX, "delta-2"), "initial": False})
    )


async def test_loading_a_vanished_message_raises_not_found(
    graph: respx.MockRouter, provider: GraphProvider
) -> None:
    link = delta_url(INBOX, "delta-1")
    graph.get(link).mock(return_value=page([message("c1")], delta=link))
    graph.get(host=GRAPH_HOST, path=path("/me/messages/c1/$value")).mock(
        return_value=graph_error(404, "ErrorItemNotFound")
    )
    events = await collect(
        provider.fetch_since(INBOX, SyncCursor({"v": 1, "link": link, "initial": False}))
    )
    changed = events[0]
    assert isinstance(changed, MessageChanged)
    with pytest.raises(MessageNotFoundError):
        await changed.load()


@pytest.mark.parametrize(
    "response",
    [
        graph_error(410, "SyncStateNotFound"),
        graph_error(400, "syncStateNotFound"),
        graph_error(400, "resyncRequired"),
    ],
)
async def test_expired_delta_token_invalidates_the_cursor(
    graph: respx.MockRouter, provider: GraphProvider, response: httpx.Response
) -> None:
    link = delta_url(INBOX, "old")
    graph.get(link).mock(return_value=response)
    with pytest.raises(CursorInvalidError):
        await collect(
            provider.fetch_since(INBOX, SyncCursor({"v": 1, "link": link, "initial": False}))
        )


@pytest.mark.parametrize("data", [{}, {"v": 2, "link": "x"}, {"v": 1, "link": ""}])
async def test_unreadable_cursor_is_invalid(provider: GraphProvider, data: dict[str, Any]) -> None:
    with pytest.raises(CursorInvalidError):
        await collect(provider.fetch_since(INBOX, SyncCursor(data)))


async def test_links_to_other_hosts_are_never_called(
    graph: respx.MockRouter, provider: GraphProvider
) -> None:
    evil = graph.get(host="evil.example").respond(json={})
    with pytest.raises(ProviderError) as info:
        await collect(
            provider.fetch_since(
                INBOX, SyncCursor({"v": 1, "link": "https://evil.example/x", "initial": False})
            )
        )
    assert info.value.code == "invalid_response"
    assert not evil.called


async def test_deleted_folder_is_a_folder_error(
    graph: respx.MockRouter, provider: GraphProvider
) -> None:
    graph.get(host=GRAPH_HOST, path=path("/me/mailFolders/gone/messages/delta")).mock(
        return_value=graph_error(404, "ErrorItemNotFound")
    )
    with pytest.raises(ProviderError) as info:
        await collect(provider.fetch_since("gone", None))
    assert info.value.code == "folder_not_found"
    assert not isinstance(info.value, MessageNotFoundError)


# -- throttling and errors ------------------------------------------------------------------


async def test_throttling_waits_for_retry_after() -> None:
    sleeps = Sleeps()
    provider = make_provider(sleep=sleeps)
    link = delta_url(INBOX, "delta-1")
    with respx.mock() as graph:
        graph.get(link).mock(
            side_effect=[
                graph_error(429, "TooManyRequests", {"Retry-After": "7"}),
                graph_error(503, "ServiceUnavailable"),
                page([], delta=link),
            ]
        )
        events = await collect(
            provider.fetch_since(INBOX, SyncCursor({"v": 1, "link": link, "initial": False}))
        )
    await provider.aclose()
    assert len(events) == 1
    # Retry-After is honoured; without it the wait grows exponentially.
    assert sleeps == [7.0, 2.0]


async def test_throttling_gives_up_after_max_retries() -> None:
    sleeps = Sleeps()
    provider = make_provider(sleep=sleeps, graph=graph_settings(max_retries=2))
    link = delta_url(INBOX, "delta-1")
    with respx.mock() as graph:
        graph.get(link).mock(
            return_value=graph_error(429, "TooManyRequests", {"Retry-After": "600"})
        )
        with pytest.raises(ConnectionFailedError) as info:
            await collect(
                provider.fetch_since(INBOX, SyncCursor({"v": 1, "link": link, "initial": False}))
            )
    await provider.aclose()
    assert info.value.code == "throttled"
    # Waits are capped.
    assert sleeps == [120.0, 120.0]


async def test_throttled_batch_parts_are_retried() -> None:
    sleeps = Sleeps()
    provider = make_provider(sleep=sleeps)
    calls: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        ids = [item["id"] for item in json.loads(request.content)["requests"]]
        calls.append(ids)
        responses = []
        for request_id in ids:
            if request_id == "1" and len(calls) == 1:
                responses.append(
                    {"id": "1", "status": 429, "headers": {"Retry-After": "3"}, "body": {}}
                )
            else:
                responses.append(mime_part(request_id, mime()))
        return httpx.Response(200, json={"responses": responses})

    with respx.mock() as graph:
        graph.get(host=GRAPH_HOST, path=path(f"/me/mailFolders/{INBOX}/messages/delta")).mock(
            return_value=page([message("a"), message("b")], delta=delta_url(INBOX, "d"))
        )
        graph.post(f"{GRAPH}/$batch").mock(side_effect=handler)
        events = await collect(provider.fetch_since(INBOX, None))
    await provider.aclose()

    assert calls == [["0", "1"], ["1"]]
    assert sleeps == [3.0]
    assert [e.message.remote_ref for e in events if isinstance(e, MessageFetched)] == ["a", "b"]


async def test_batches_hold_at_most_twenty_requests(graph: respx.MockRouter) -> None:
    provider = make_provider(batch_size=45)
    sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        items = json.loads(request.content)["requests"]
        sizes.append(len(items))
        return httpx.Response(
            200, json={"responses": [mime_part(item["id"], mime()) for item in items]}
        )

    graph.get(host=GRAPH_HOST, path=path(f"/me/mailFolders/{INBOX}/messages/delta")).mock(
        return_value=page([message(f"m{i}") for i in range(45)], delta=delta_url(INBOX, "d"))
    )
    graph.post(f"{GRAPH}/$batch").mock(side_effect=handler)
    events = await collect(provider.fetch_since(INBOX, None))
    await provider.aclose()
    assert sizes == [20, 20, 5]
    assert len([e for e in events if isinstance(e, MessageFetched)]) == 45


async def test_network_errors_are_connection_failures(
    graph: respx.MockRouter, provider: GraphProvider
) -> None:
    graph.get(host=GRAPH_HOST).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(ConnectionFailedError):
        await provider.verify()


async def test_errors_carry_no_server_text(
    graph: respx.MockRouter, provider: GraphProvider
) -> None:
    graph.get(host=GRAPH_HOST).mock(return_value=graph_error(400, "ErrorInvalidRequest"))
    with pytest.raises(ProviderError) as info:
        await provider.verify()
    assert info.value.code == "request_failed"
    assert "erika" not in str(info.value) and "Synthetic" not in repr(info.value)


# -- tokens ---------------------------------------------------------------------------------


async def test_expired_access_token_is_refreshed_and_saved(graph: respx.MockRouter) -> None:
    saved: list[dict[str, Any]] = []

    async def save(credentials: dict[str, Any]) -> None:
        saved.append(credentials)

    provider = make_provider(creds=credentials(expires_at=NOW + 60), save=save)
    token = graph.post(TOKEN_URL).mock(return_value=token_response())
    inbox = graph.get(host=GRAPH_HOST, path=path("/me/mailFolders/inbox")).respond(
        json={"id": INBOX}
    )

    await provider.verify()
    await provider.verify()
    await provider.aclose()

    assert token.call_count == 1
    sent = form(token.calls[0].request)
    assert sent["grant_type"] == "refresh_token"
    assert sent["refresh_token"] == "refresh-1"
    assert sent["client_secret"] == "test-client-secret"
    assert "offline_access" in sent["scope"].split()
    assert "https://graph.microsoft.com/Mail.ReadWrite" in sent["scope"].split()
    assert inbox.calls[0].request.headers["Authorization"] == "Bearer access-2"
    assert saved == [
        {"access_token": "access-2", "refresh_token": "refresh-2", "expires_at": int(NOW) + 3599}
    ]


async def test_unrotated_refresh_token_is_kept(graph: respx.MockRouter) -> None:
    saved: list[dict[str, Any]] = []

    async def save(credentials: dict[str, Any]) -> None:
        saved.append(credentials)

    provider = make_provider(creds=credentials(expires_at=0), save=save)
    graph.post(TOKEN_URL).mock(return_value=token_response(refresh=None))
    graph.get(host=GRAPH_HOST).respond(json={"id": INBOX})
    await provider.verify()
    await provider.aclose()
    assert saved[0]["refresh_token"] == "refresh-1"


async def test_unauthorized_response_refreshes_once(
    graph: respx.MockRouter, provider: GraphProvider
) -> None:
    token = graph.post(TOKEN_URL).mock(return_value=token_response())
    inbox = graph.get(host=GRAPH_HOST, path=path("/me/mailFolders/inbox")).mock(
        side_effect=[graph_error(401, "InvalidAuthenticationToken"), httpx.Response(200, json={})]
    )
    await provider.verify()
    assert token.call_count == 1
    assert [c.request.headers["Authorization"] for c in inbox.calls] == [
        "Bearer access-1",
        "Bearer access-2",
    ]


async def test_still_unauthorized_after_refresh_is_an_authentication_error(
    graph: respx.MockRouter, provider: GraphProvider
) -> None:
    graph.post(TOKEN_URL).mock(return_value=token_response())
    graph.get(host=GRAPH_HOST).mock(return_value=graph_error(401, "InvalidAuthenticationToken"))
    with pytest.raises(AuthenticationError):
        await provider.verify()


@pytest.mark.parametrize("error", ["invalid_grant", "invalid_client", "interaction_required"])
async def test_rejected_refresh_token_is_an_authentication_error(
    graph: respx.MockRouter, error: str
) -> None:
    provider = make_provider(creds=credentials(expires_at=0))
    graph.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            400, json={"error": error, "error_description": "AADSTS70000: erika@example.com"}
        )
    )
    with pytest.raises(AuthenticationError) as info:
        await provider.verify()
    await provider.aclose()
    assert "erika" not in str(info.value)


async def test_token_endpoint_outage_is_a_connection_failure(graph: respx.MockRouter) -> None:
    provider = make_provider(creds=credentials(expires_at=0))
    graph.post(TOKEN_URL).mock(return_value=httpx.Response(503))
    with pytest.raises(ConnectionFailedError):
        await provider.verify()
    await provider.aclose()


async def test_missing_refresh_token_needs_reconnect(graph: respx.MockRouter) -> None:
    provider = make_provider(creds={})
    with pytest.raises(AuthenticationError):
        await provider.verify()
    await provider.aclose()


async def test_app_only_token_is_cached_per_process(graph: respx.MockRouter) -> None:
    token = graph.post(TOKEN_URL).mock(return_value=token_response("app-token", refresh=None))
    folder = graph.get(
        host=GRAPH_HOST, path=path("/users/team@example.com/mailFolders/inbox")
    ).respond(json={"id": INBOX})

    for _ in range(2):
        provider = make_provider(
            settings={"auth": "application"}, creds={}, address="team@example.com"
        )
        await provider.verify()
        await provider.aclose()

    assert token.call_count == 1
    sent = form(token.calls[0].request)
    assert sent["grant_type"] == "client_credentials"
    assert sent["scope"] == "https://graph.microsoft.com/.default"
    assert folder.calls[0].request.headers["Authorization"] == "Bearer app-token"


async def test_app_only_needs_a_tenant_id() -> None:
    with pytest.raises(ConfigurationError) as info:
        make_provider(
            settings={"auth": "application", "user": "team@example.com"},
            graph=graph_settings(tenant_id="organizations"),
        )
    assert info.value.code == "graph_tenant_required"


async def test_app_only_cannot_use_me() -> None:
    with pytest.raises(ConfigurationError):
        make_provider(settings={"auth": "application", "user": "me"})


@pytest.mark.parametrize(
    ("user", "shared"), [("team@example.com", True), ("Erika@example.com", False)]
)
async def test_delegated_mailbox_by_address_uses_the_users_path(
    graph: respx.MockRouter, user: str, shared: bool
) -> None:
    provider = make_provider(
        settings={"auth": "delegated", "user": user}, creds=credentials(expires_at=0)
    )
    token = graph.post(TOKEN_URL).mock(return_value=token_response())
    route = graph.get(host=GRAPH_HOST, path=path(f"/users/{user}/mailFolders/inbox")).respond(
        json={"id": INBOX}
    )
    await provider.verify()
    await provider.aclose()
    assert route.called
    scopes = form(token.calls[0].request)["scope"].split()
    assert ("https://graph.microsoft.com/Mail.ReadWrite.Shared" in scopes) is shared


def test_disabled_without_client_id() -> None:
    with pytest.raises(ConfigurationError) as info:
        make_provider(graph=graph_settings(client_id=None))
    assert info.value.code == "graph_not_configured"


def test_registered_for_graph_mailboxes() -> None:
    from app.mail.models import MailboxType

    assert registry.is_registered(MailboxType.GRAPH)
    assert make_provider().capabilities.server_threads
    assert not make_provider().capabilities.push
    assert make_provider(
        graph=graph_settings(notification_url="https://mail.example.com/api/x")
    ).capabilities.push


# -- actions --------------------------------------------------------------------------------


async def test_move_keeps_the_immutable_id(
    graph: respx.MockRouter, provider: GraphProvider
) -> None:
    route = graph.post(host=GRAPH_HOST, path=path("/me/messages/m1/move")).respond(
        201, json=message("m1", ARCHIVE)
    )
    assert await provider.move("m1", ARCHIVE) == "m1"
    assert json.loads(route.calls[0].request.content) == {"destinationId": ARCHIVE}
    assert 'IdType="ImmutableId"' in route.calls[0].request.headers["Prefer"]


async def test_set_flags_maps_to_read_flag_and_categories(
    graph: respx.MockRouter, provider: GraphProvider
) -> None:
    route = graph.patch(host=GRAPH_HOST, path=path("/me/messages/m1")).respond(json={})
    await provider.set_flags("m1", frozenset({Flag.SEEN, "Projekt A", Flag.ANSWERED}))
    await provider.set_flags("m1", frozenset({Flag.FLAGGED}))
    bodies = [json.loads(call.request.content) for call in route.calls]
    assert bodies == [
        {"isRead": True, "flag": {"flagStatus": "notFlagged"}, "categories": ["Projekt A"]},
        {"isRead": False, "flag": {"flagStatus": "flagged"}, "categories": []},
    ]


async def test_labels_are_categories(graph: respx.MockRouter, provider: GraphProvider) -> None:
    graph.get(host=GRAPH_HOST, path=path("/me/messages/m1")).respond(json={"categories": ["Rot"]})
    patch = graph.patch(host=GRAPH_HOST, path=path("/me/messages/m1")).respond(json={})

    await provider.apply_label("m1", "Projekt A")
    await provider.apply_label("m1", "Rot")
    await provider.remove_label("m1", "Rot")
    await provider.remove_label("m1", "Unbekannt")

    bodies = [json.loads(call.request.content) for call in patch.calls]
    assert bodies == [{"categories": ["Rot", "Projekt A"]}, {"categories": []}]


async def test_actions_on_missing_messages(
    graph: respx.MockRouter, provider: GraphProvider
) -> None:
    graph.route(host=GRAPH_HOST).mock(return_value=graph_error(404, "ErrorItemNotFound"))
    with pytest.raises(MessageNotFoundError):
        await provider.move("m1", ARCHIVE)
    with pytest.raises(MessageNotFoundError):
        await provider.set_flags("m1", frozenset())
    with pytest.raises(MessageNotFoundError):
        await provider.apply_label("m1", "x")


# -- push -----------------------------------------------------------------------------------


async def test_watch_without_notification_url_falls_back_to_polling(
    provider: GraphProvider,
) -> None:
    with pytest.raises(NotImplementedError):
        await anext(provider.watch())


async def test_watch_keeps_a_subscription_alive(graph: respx.MockRouter) -> None:
    mailbox_id = uuid.uuid4()
    sleeps = Sleeps()
    url = "https://mail.example.com/api/mail/graph/notifications"
    provider = make_provider(
        graph=graph_settings(notification_url=url), mailbox_id=mailbox_id, sleep=sleeps
    )
    create = graph.post(f"{GRAPH}/subscriptions").mock(
        side_effect=[
            httpx.Response(201, json={"id": "sub-1"}),
            httpx.Response(201, json={"id": "sub-2"}),
        ]
    )
    renew = graph.patch(f"{GRAPH}/subscriptions/sub-1").mock(
        side_effect=[httpx.Response(200, json={}), graph_error(404, "ResourceNotFound")]
    )
    delete = graph.delete(f"{GRAPH}/subscriptions/sub-2").respond(204)

    events = provider.watch()
    assert await anext(events) == ChangeEvent(None)
    body = json.loads(create.calls[0].request.content)
    assert body["resource"] == "me/messages"
    assert body["changeType"] == "created,updated,deleted"
    assert body["notificationUrl"] == url and body["lifecycleNotificationUrl"] == url
    assert body["expirationDateTime"] == "2026-09-23T14:13:20Z"
    assert verify_client_state(SECURITY, body["clientState"]) == mailbox_id
    assert "includeResourceData" not in body

    # Renewed after a day; a vanished subscription is created again.
    assert await anext(events) == ChangeEvent(None)
    assert sleeps == [86400.0, 86400.0]
    assert renew.call_count == 2 and create.call_count == 2

    await events.aclose()
    assert delete.called
    await provider.aclose()


async def test_watch_falls_back_to_polling_if_graph_rejects_the_subscription(
    graph: respx.MockRouter,
) -> None:
    provider = make_provider(graph=graph_settings(notification_url="https://m.example.com/n"))
    graph.post(f"{GRAPH}/subscriptions").mock(
        return_value=graph_error(400, "InvalidRequest")  # e.g. URL validation failed
    )
    with pytest.raises(NotImplementedError):
        await anext(provider.watch())
    await provider.aclose()


async def test_watch_is_cancelled_cleanly(graph: respx.MockRouter) -> None:
    provider = make_provider(graph=graph_settings(notification_url="https://m.example.com/n"))
    graph.post(f"{GRAPH}/subscriptions").respond(201, json={"id": "sub-1"})
    delete = graph.delete(f"{GRAPH}/subscriptions/sub-1").respond(204)
    provider._sleep = asyncio.sleep  # type: ignore[assignment]

    async def consume() -> None:
        async for _ in provider.watch():
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert delete.called
    await provider.aclose()
