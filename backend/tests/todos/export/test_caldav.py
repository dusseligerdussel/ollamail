"""CalDAV sink against a real CalDAV server (Radicale) plus recorded edge cases (respx)."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime

import httpx
import pytest
import respx

from app.todos.export.base import (
    SinkAuthError,
    SinkConflictError,
    SinkError,
    SinkUnavailableError,
    TaskData,
)
from app.todos.export.caldav import CalDAVSink, validate_server_url
from app.todos.export.ical import parse_vtodo
from app.todos.models import TodoPriority, TodoStatus
from tests.todos.export.conftest import CALDAV_USERS, CalDAVServer

MODIFIED = datetime(2026, 10, 7, 9, 30, tzinfo=UTC)
# Radicale listens on loopback, which the destination check refuses without an entry.
LOCAL = ("127.0.0.0/8",)


def task(uid: str | None = None, **changes: object) -> TaskData:
    values: dict[str, object] = {
        "uid": uid or str(uuid.uuid4()),
        "title": "Send the quarterly report",
        "description": "Numbers for Q3, see mail",
        "due_date": date(2026, 10, 9),
        "priority": TodoPriority.HIGH,
        "status": TodoStatus.OPEN,
        "completed_at": None,
        "modified_at": MODIFIED,
        "url": "https://mail.example.org/inbox?message=0192",
    }
    values.update(changes)
    return TaskData(**values)  # type: ignore[arg-type]


@pytest.fixture
async def sink(caldav_server: CalDAVServer) -> AsyncIterator[CalDAVSink]:
    sink = CalDAVSink(
        caldav_server.url + "/",
        "erika",
        CALDAV_USERS["erika"],
        allow_http=True,
        allowed_internal_hosts=LOCAL,
        timeout=10,
    )
    yield sink
    await sink.aclose()


async def test_discovers_task_lists_from_the_server_root(caldav_server: CalDAVServer) -> None:
    caldav_server.make_calendar("lena", "tasks")
    caldav_server.make_calendar("lena", "events", ("VEVENT",))
    caldav_server.make_calendar("lena", "mixed", ("VEVENT", "VTODO"))
    for url in (caldav_server.url, caldav_server.user_url("lena")):
        sink = CalDAVSink(
            url, "lena", CALDAV_USERS["lena"], allow_http=True, allowed_internal_hosts=LOCAL
        )
        try:
            lists = await sink.list_task_lists()
        finally:
            await sink.aclose()

        # Calendars that cannot hold tasks are not offered.
        assert [(item.id, item.name) for item in lists] == [
            ("/lena/mixed/", "Mixed"),
            ("/lena/tasks/", "Tasks"),
        ]


async def test_a_calendar_url_is_its_own_list(caldav_server: CalDAVServer, calendar: str) -> None:
    sink = CalDAVSink(
        caldav_server.url + calendar,
        "erika",
        CALDAV_USERS["erika"],
        allow_http=True,
        allowed_internal_hosts=LOCAL,
    )
    try:
        lists = await sink.list_task_lists()
    finally:
        await sink.aclose()

    assert [item.id for item in lists] == [calendar]


async def test_wrong_password_is_an_auth_error(caldav_server: CalDAVServer) -> None:
    sink = CalDAVSink(
        caldav_server.url, "erika", "wrong", allow_http=True, allowed_internal_hosts=LOCAL
    )
    try:
        with pytest.raises(SinkAuthError):
            await sink.list_task_lists()
    finally:
        await sink.aclose()


async def test_push_writes_a_vtodo_with_the_mail_link(
    sink: CalDAVSink, caldav_server: CalDAVServer, calendar: str
) -> None:
    data = task()

    version = await sink.push(calendar, data)

    assert version.remote_id == f"{calendar}{data.uid}.ics"
    assert version.etag
    body = caldav_server.get(version.remote_id).text
    assert "SUMMARY:Send the quarterly report" in body
    assert "DUE;VALUE=DATE:20261009" in body
    assert "PRIORITY:1" in body
    assert "URL:https://mail.example.org/inbox?message=0192" in body
    assert "Numbers for Q3\\, see mail\\n\\nhttps://mail.example.org/inbox?message=0192" in (
        body.replace("\r\n ", "")
    )
    assert parse_vtodo(body).status == TodoStatus.OPEN


async def test_pushing_twice_overwrites_instead_of_duplicating(
    sink: CalDAVSink, calendar: str
) -> None:
    data = task()
    first = await sink.push(calendar, data)

    second = await sink.push(calendar, task(data.uid, title="Renamed"))

    assert second.remote_id == first.remote_id
    assert second.etag != first.etag
    assert await sink.changes(calendar, {first.remote_id: second.etag}) == {}


async def test_update_needs_the_current_etag(sink: CalDAVSink, calendar: str) -> None:
    data = task()
    first = await sink.push(calendar, data)
    second = await sink.update(calendar, first.remote_id, first.etag, task(data.uid, title="B"))

    with pytest.raises(SinkConflictError):
        await sink.update(calendar, first.remote_id, first.etag, task(data.uid, title="C"))
    done = await sink.complete(
        calendar,
        first.remote_id,
        second.etag,
        task(data.uid, status=TodoStatus.DONE, completed_at=MODIFIED),
    )
    assert done.etag not in {first.etag, second.etag}


async def test_changes_reports_status_set_in_the_target_and_deletions(
    sink: CalDAVSink, caldav_server: CalDAVServer, calendar: str
) -> None:
    kept = await sink.push(calendar, task())
    edited = await sink.push(calendar, task())
    gone = await sink.push(calendar, task())
    # Another client completes one task and deletes another.
    body = caldav_server.get(edited.remote_id).text
    body = body.replace("STATUS:NEEDS-ACTION", "STATUS:COMPLETED").replace(
        "LAST-MODIFIED:20261007T093000Z", "LAST-MODIFIED:20261008T120000Z"
    )
    assert caldav_server.put(edited.remote_id, body).status_code in {201, 204}
    assert caldav_server.delete(gone.remote_id).status_code in {200, 204}

    changes = await sink.changes(
        calendar,
        {kept.remote_id: kept.etag, edited.remote_id: edited.etag, gone.remote_id: gone.etag},
    )

    assert set(changes) == {edited.remote_id, gone.remote_id}
    assert changes[gone.remote_id] is None
    remote = changes[edited.remote_id]
    assert remote is not None
    assert remote.status == TodoStatus.DONE
    assert remote.modified_at == datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    assert remote.etag and remote.etag != edited.etag


async def test_delete_is_idempotent(
    sink: CalDAVSink, caldav_server: CalDAVServer, calendar: str
) -> None:
    version = await sink.push(calendar, task())

    await sink.delete(calendar, version.remote_id)
    await sink.delete(calendar, version.remote_id)

    assert caldav_server.get(version.remote_id).status_code == 404


async def test_requests_never_leave_the_configured_server(sink: CalDAVSink) -> None:
    with pytest.raises(SinkError) as raised:
        await sink.delete("/x/", "relative/path.ics")
    assert raised.value.code == "invalid_id"


@pytest.mark.parametrize(
    ("url", "allow_http", "code"),
    [
        ("http://dav.example.org/", False, "insecure_url"),
        ("ftp://dav.example.org/", True, "invalid_url"),
        ("https://user:pw@dav.example.org/", False, "invalid_url"),
        ("https:///nohost", False, "invalid_url"),
    ],
)
def test_server_url_validation(url: str, allow_http: bool, code: str) -> None:
    with pytest.raises(SinkError) as raised:
        validate_server_url(url, allow_http=allow_http)
    assert raised.value.code == code


def test_valid_server_url_drops_query() -> None:
    assert (
        validate_server_url(" https://dav.example.org/remote.php/dav?x=1 ", allow_http=False)
        == "https://dav.example.org/remote.php/dav"
    )


@respx.mock
async def test_redirects_to_other_hosts_are_refused() -> None:
    respx.route(method="PROPFIND", url="https://dav.example.org/").mock(
        return_value=httpx.Response(301, headers={"Location": "https://evil.example.com/"})
    )
    sink = CalDAVSink("https://dav.example.org/", "erika", "x")
    try:
        with pytest.raises(SinkError) as raised:
            await sink.list_task_lists()
    finally:
        await sink.aclose()
    assert raised.value.code == "not_caldav"


@respx.mock
async def test_well_known_redirect_on_the_same_host_is_followed() -> None:
    multistatus = (
        '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" '
        'xmlns:c="urn:ietf:params:xml:ns:caldav"><d:response><d:href>{href}</d:href>'
        "<d:propstat><d:prop>{props}</d:prop><d:status>HTTP/1.1 200 OK</d:status>"
        "</d:propstat></d:response></d:multistatus>"
    )
    respx.route(method="PROPFIND", url="https://dav.example.org/").mock(
        return_value=httpx.Response(207, text=multistatus.format(href="/", props=""))
    )
    respx.route(method="PROPFIND", url="https://dav.example.org/.well-known/caldav").mock(
        return_value=httpx.Response(301, headers={"Location": "/dav/"})
    )
    respx.route(method="PROPFIND", url="https://dav.example.org/dav/").mock(
        return_value=httpx.Response(
            207,
            text=multistatus.format(
                href="/dav/",
                props="<c:calendar-home-set><d:href>/dav/cal/</d:href></c:calendar-home-set>",
            ),
        )
    )
    respx.route(method="PROPFIND", url="https://dav.example.org/dav/cal/").mock(
        return_value=httpx.Response(
            207,
            text=multistatus.format(
                href="/dav/cal/t%C3%A4sks/",
                props="<d:resourcetype><d:collection/><c:calendar/></d:resourcetype>"
                "<d:displayname>Aufgaben</d:displayname>",
            ),
        )
    )
    sink = CalDAVSink("https://dav.example.org", "erika", "x")
    try:
        lists = await sink.list_task_lists()
    finally:
        await sink.aclose()

    assert [(item.id, item.name) for item in lists] == [("/dav/cal/täsks/", "Aufgaben")]


@respx.mock
async def test_a_redirect_to_a_calendar_makes_the_target_the_list() -> None:
    respx.route(method="PROPFIND", url="https://dav.example.org/tasks").mock(
        return_value=httpx.Response(301, headers={"Location": "/dav/cal/t%C3%A4sks/"})
    )
    respx.route(method="PROPFIND", url="https://dav.example.org/dav/cal/t%C3%A4sks/").mock(
        return_value=httpx.Response(
            207,
            text='<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" '
            'xmlns:c="urn:ietf:params:xml:ns:caldav"><d:response>'
            "<d:href>/dav/cal/t%C3%A4sks/</d:href><d:propstat><d:prop>"
            "<d:resourcetype><d:collection/><c:calendar/></d:resourcetype>"
            "<d:displayname>Aufgaben</d:displayname></d:prop>"
            "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>",
        )
    )
    put = respx.route(method="PUT").mock(return_value=httpx.Response(201, headers={"ETag": '"1"'}))
    sink = CalDAVSink("https://dav.example.org/tasks", "erika", "x")
    try:
        lists = await sink.list_task_lists()
        version = await sink.push(lists[0].id, task("abc"))
    finally:
        await sink.aclose()

    assert [(item.id, item.name) for item in lists] == [("/dav/cal/täsks/", "Aufgaben")]
    # Writes go straight to the target: PUT does not follow redirects.
    assert put.calls.last.request.url == "https://dav.example.org/dav/cal/t%C3%A4sks/abc.ics"
    assert version.remote_id == "/dav/cal/täsks/abc.ics"


@respx.mock
async def test_entity_declarations_are_rejected() -> None:
    respx.route(method="PROPFIND").mock(
        return_value=httpx.Response(
            207,
            text='<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>',
        )
    )
    sink = CalDAVSink("https://dav.example.org/", "erika", "x")
    try:
        with pytest.raises(SinkError) as raised:
            await sink.list_task_lists()
    finally:
        await sink.aclose()
    assert raised.value.code == "not_caldav"


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(503), SinkUnavailableError),
        (httpx.Response(429), SinkUnavailableError),
        (httpx.Response(403), SinkAuthError),
        (httpx.ConnectError("refused"), SinkUnavailableError),
    ],
)
@respx.mock
async def test_errors_map_to_codes(
    response: httpx.Response | Exception, error: type[SinkError]
) -> None:
    route = respx.route(method="PUT")
    if isinstance(response, Exception):
        route.mock(side_effect=response)
    else:
        route.mock(return_value=response)
    sink = CalDAVSink("https://dav.example.org/", "erika", "x")
    try:
        with pytest.raises(error):
            await sink.push("/erika/tasks/", task())
    finally:
        await sink.aclose()
