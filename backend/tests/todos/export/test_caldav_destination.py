"""Destination check of the CalDAV export (#189): internal addresses are refused like
unreachable servers unless ``OLLAMAIL_TODOS_EXPORT_ALLOWED_INTERNAL_HOSTS`` allows them,
connections go to the checked address and errors do not reveal what answered.

Host names are invented, DNS answers are stubbed and "public" addresses are routed to a
local test server, so nothing leaves the machine."""

import asyncio
import socket
import typing
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpcore
import httpx
import pytest
import respx
from pydantic import ValidationError

from app.core.config import TodosSettings
from app.core.http_guard import GuardedTransport
from app.todos.export.base import SinkAuthError, SinkError, SinkUnavailableError
from app.todos.export.caldav import CalDAVSink
from app.todos.export.registry import create_sink

PUBLIC = "93.184.215.14"
EVENT = "todo_export_destination_refused"


def answers(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, list[list[str]]]) -> None:
    """Stub DNS: host name → one list of addresses per lookup (the last one repeats)."""

    async def getaddrinfo(host: str, port: int, **kwargs: Any) -> list[tuple[Any, ...]]:
        rounds = mapping.get(host.lower())
        if rounds is None:
            # IP literals resolve to themselves, as with the real resolver.
            try:
                socket.inet_pton(socket.AF_INET6 if ":" in host else socket.AF_INET, host)
            except OSError:
                raise socket.gaierror(socket.EAI_NONAME, "unknown") from None
            current = [host]
        else:
            current = rounds.pop(0) if len(rounds) > 1 else rounds[0]
        result: list[tuple[Any, ...]] = []
        for address in current:
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            sockaddr = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
            result.append((family, socket.SOCK_STREAM, 6, "", sockaddr))
        return result

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", getaddrinfo)


class Routed(httpcore.AsyncNetworkBackend):
    """Records the addresses connected to; ``routes`` sends some to local ports."""

    def __init__(self, routes: dict[str, int] | None = None) -> None:
        self.routes = routes or {}
        self.connected: list[str] = []
        self._real = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 (httpcore interface)
        local_address: str | None = None,
        socket_options: typing.Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self.connected.append(host)
        if host not in self.routes:
            raise httpcore.ConnectError()
        return await self._real.connect_tcp("127.0.0.1", self.routes[host], timeout=timeout)

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109 (httpcore interface)
        socket_options: typing.Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


def guarded_sink(
    url: str, backend: Routed, allowed: tuple[str, ...] = (), *, allow_http: bool = False
) -> CalDAVSink:
    transport = GuardedTransport(allowed, log_event=EVENT, backend=backend)
    return CalDAVSink(url, "erika", "secret", allow_http=allow_http, transport=transport)


async def discover(sink: CalDAVSink) -> None:
    try:
        await sink.list_task_lists()
    finally:
        await sink.aclose()


MULTISTATUS = (
    '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" '
    'xmlns:c="urn:ietf:params:xml:ns:caldav"><d:response><d:href>/dav/tasks/</d:href>'
    "<d:propstat><d:prop><d:resourcetype><d:collection/><c:calendar/></d:resourcetype>"
    "<d:displayname>Aufgaben</d:displayname></d:prop>"
    "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>"
)


@dataclass
class DavServer:
    """Minimal HTTP server: ``/`` redirects to ``/dav/tasks/`` (new connection), which is a
    task list. Records the request lines and ``Host`` headers."""

    port: int = 0
    requests: list[tuple[str, str]] = field(default_factory=list)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        lines = head.decode().split("\r\n")
        headers = {
            name.lower(): value.strip()
            for name, _, value in (line.partition(":") for line in lines[1:] if line)
        }
        await reader.readexactly(int(headers.get("content-length", "0")))
        path = lines[0].split()[1]
        self.requests.append((path, headers.get("host", "")))
        if path == "/":
            response = b"HTTP/1.1 301 Moved\r\nLocation: /dav/tasks/\r\n"
            response += b"Content-Length: 0\r\nConnection: close\r\n\r\n"
        else:
            body = MULTISTATUS.encode()
            response = b"HTTP/1.1 207 Multi-Status\r\nContent-Type: application/xml\r\n"
            response += f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            response += body
        writer.write(response)
        await writer.drain()
        writer.close()


@pytest.fixture
async def dav_server() -> AsyncIterator[DavServer]:
    server = DavServer()
    listener = await asyncio.start_server(server.handle, "127.0.0.1", 0)
    server.port = listener.sockets[0].getsockname()[1]
    async with listener:
        yield server


@pytest.mark.parametrize(
    "addresses",
    [
        ["127.0.0.1"],
        ["10.0.0.5"],
        ["172.16.4.2"],
        ["192.168.1.20"],
        ["169.254.169.254"],
        ["100.64.0.1"],
        ["::1"],
        ["fe80::1"],
        ["fd12:3456::7"],
        ["::ffff:10.0.0.1"],
        ["192.168.0.2", "fd00::25"],
    ],
)
async def test_internal_destinations_are_refused_like_unreachable_ones(
    monkeypatch: pytest.MonkeyPatch, addresses: list[str]
) -> None:
    answers(monkeypatch, {"dav.internal.test": [addresses]})
    backend = Routed()

    with pytest.raises(SinkUnavailableError) as refused:
        await discover(guarded_sink("https://dav.internal.test/", backend))
    with pytest.raises(SinkUnavailableError) as unknown:
        await discover(guarded_sink("https://missing.internal.test/", backend))

    assert refused.value.code == unknown.value.code == "unavailable"
    assert backend.connected == []


@pytest.mark.parametrize(
    "url", ["https://10.0.0.5:8443/", "https://[fd00::1]/", "https://127.0.0.1/"]
)
async def test_internal_ip_literals_are_refused(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    answers(monkeypatch, {})
    backend = Routed()

    with pytest.raises(SinkUnavailableError):
        await discover(guarded_sink(url, backend))
    assert backend.connected == []


async def test_refusals_are_logged_without_host_or_address(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    answers(monkeypatch, {"dav.internal.test": [["10.9.8.7"]]})

    with pytest.raises(SinkUnavailableError):
        await discover(guarded_sink("https://dav.internal.test/", Routed()))

    output = caplog.text
    assert EVENT in output
    assert "dav.internal.test" not in output
    assert "10.9.8.7" not in output


async def test_connects_to_the_checked_address_and_keeps_the_host_name(
    monkeypatch: pytest.MonkeyPatch, dav_server: DavServer
) -> None:
    answers(monkeypatch, {"dav.example.test": [[PUBLIC]]})
    backend = Routed({PUBLIC: dav_server.port})
    sink = guarded_sink(
        f"http://dav.example.test:{dav_server.port}/dav/tasks/", backend, allow_http=True
    )
    try:
        lists = await sink.list_task_lists()
    finally:
        await sink.aclose()

    assert [item.name for item in lists] == ["Aufgaben"]
    assert backend.connected == [PUBLIC]
    assert dav_server.requests == [("/dav/tasks/", f"dav.example.test:{dav_server.port}")]


async def test_dns_rebinding_on_a_redirect_is_refused(
    monkeypatch: pytest.MonkeyPatch, dav_server: DavServer
) -> None:
    # First answer public, the second one (connection after the redirect) internal.
    answers(monkeypatch, {"dav.rebind.test": [[PUBLIC], ["127.0.0.1"]]})
    backend = Routed({PUBLIC: dav_server.port, "127.0.0.1": dav_server.port})

    with pytest.raises(SinkUnavailableError):
        await discover(
            guarded_sink(f"http://dav.rebind.test:{dav_server.port}/", backend, allow_http=True)
        )

    assert backend.connected == [PUBLIC]
    assert [path for path, _ in dav_server.requests] == ["/"]


@pytest.mark.parametrize("allowed", [("dav.lan.test",), ("10.20.0.0/16",), ("10.20.1.2",)])
async def test_allowlisted_internal_servers_are_reached(
    monkeypatch: pytest.MonkeyPatch, dav_server: DavServer, allowed: tuple[str, ...]
) -> None:
    answers(monkeypatch, {"dav.lan.test": [["10.20.1.2"]]})
    backend = Routed({"10.20.1.2": dav_server.port})
    sink = guarded_sink(
        f"http://dav.lan.test:{dav_server.port}/", backend, allowed, allow_http=True
    )
    try:
        lists = await sink.list_task_lists()
    finally:
        await sink.aclose()

    # Redirect followed on a new connection, checked again.
    assert [item.name for item in lists] == ["Aufgaben"]
    assert backend.connected == ["10.20.1.2", "10.20.1.2"]


async def test_allowlist_covers_only_listed_ranges(monkeypatch: pytest.MonkeyPatch) -> None:
    answers(monkeypatch, {"dav.lan.test": [["10.30.0.1"]]})
    backend = Routed()

    with pytest.raises(SinkUnavailableError):
        await discover(guarded_sink("https://dav.lan.test/", backend, ("10.20.0.0/16",)))
    assert backend.connected == []


async def test_public_address_is_chosen_when_the_name_also_resolves_internally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers(monkeypatch, {"dav.mixed.test": [["10.0.0.1", PUBLIC]]})
    backend = Routed()

    with pytest.raises(SinkUnavailableError):
        await discover(guarded_sink("https://dav.mixed.test/", backend))
    assert backend.connected == [PUBLIC]


def test_sinks_from_settings_use_the_allowlist() -> None:
    settings = TodosSettings(export_allowed_internal_hosts=["dav.lan.test", "10.0.0.0/8"])
    sink = create_sink(
        "caldav", {"url": "https://dav.lan.test/", "username": "u", "password": "p"}, settings
    )
    assert isinstance(sink, CalDAVSink)
    transport = sink._client._transport
    assert isinstance(transport, GuardedTransport)


def test_environment_proxies_are_not_used(monkeypatch: pytest.MonkeyPatch) -> None:
    # A proxy would connect in our place and skip the destination check.
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal.test:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.internal.test:3128")
    sink = CalDAVSink("https://dav.example.test/", "u", "p")
    assert isinstance(sink._client._transport, GuardedTransport)
    assert not sink._client._mounts


def test_allowlist_setting_is_parsed_and_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMAIL_TODOS_EXPORT_ALLOWED_INTERNAL_HOSTS", " DAV.lan. , 10.0.0.0/8")
    assert TodosSettings().export_allowed_internal_hosts == ["dav.lan", "10.0.0.0/8"]
    with pytest.raises(ValidationError):
        TodosSettings(export_allowed_internal_hosts=["10.0.0.0/8 dav"])


@pytest.mark.parametrize(
    ("response", "error", "code"),
    [
        (httpx.Response(400), SinkError, "not_caldav"),
        (httpx.Response(418), SinkError, "not_caldav"),
        (httpx.Response(200, text="<html>router login</html>"), SinkError, "not_caldav"),
        (httpx.Response(207, text="not xml"), SinkError, "not_caldav"),
        (httpx.Response(302, headers={"Location": "https://other.test/"}), SinkError, "not_caldav"),
        (httpx.Response(401), SinkAuthError, "auth_failed"),
        (httpx.ConnectTimeout("slow"), SinkUnavailableError, "unavailable"),
        (httpx.ReadTimeout("slow"), SinkUnavailableError, "unavailable"),
    ],
)
@respx.mock
async def test_errors_reveal_no_status_codes(
    response: httpx.Response | Exception, error: type[SinkError], code: str
) -> None:
    respx.route(method="PROPFIND").mock(
        side_effect=response if isinstance(response, Exception) else None,
        return_value=None if isinstance(response, Exception) else response,
    )
    sink = CalDAVSink("https://dav.example.org/dav/", "erika", "x")

    with pytest.raises(error) as raised:
        await discover(sink)
    assert raised.value.code == code
