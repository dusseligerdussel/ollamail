"""Destination check for mail servers (``app.mail.providers.network``): internal addresses
are refused like unreachable servers unless allowed, and connections go to the checked
address. Host names are invented; DNS answers are stubbed."""

import asyncio
import ipaddress
import socket
import uuid
from typing import Any

import pytest

from app.core.config import MailSettings
from app.mail.models import MailboxType
from app.mail.providers import network
from app.mail.providers.base import ConnectionFailedError, MailboxConfig
from app.mail.providers.imap import ImapProvider
from app.mail.providers.imap_client import ImapConnection
from app.mail.providers.smtp import SmtpTarget, _open

STRICT = MailSettings(allowed_internal_hosts=[], allow_insecure_connections=True)


def answers(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, list[str]]) -> None:
    """Stub DNS of the running loop: ``mapping`` host name → addresses."""

    async def getaddrinfo(host: str, port: int, **kwargs: Any) -> list[tuple[Any, ...]]:
        if host.lower() not in mapping:
            raise socket.gaierror(socket.EAI_NONAME, "unknown")
        result: list[tuple[Any, ...]] = []
        for address in mapping[host.lower()]:
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            sockaddr = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
            result.append((family, socket.SOCK_STREAM, 6, "", sockaddr))
        return result

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", getaddrinfo)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.5",
        "192.168.1.10",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",
        "224.0.0.1",
        "::1",
        "fe80::1",
        "fd12:3456::1",
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
        # NAT64 (well-known prefix) and IPv4-compatible addresses embed an IPv4 destination.
        "64:ff9b::7f00:1",
        "64:ff9b::a9fe:a9fe",
        "64:ff9b::a00:1",
        "::7f00:1",
        "::a00:1",
        "::c0a8:10a",
    ],
)
def test_internal_addresses_are_not_public(address: str) -> None:
    assert not network.is_public(ipaddress.ip_address(address))


@pytest.mark.parametrize(
    "address",
    ["93.184.215.14", "2a00:1450:4001:80b::200e", "1.1.1.1", "64:ff9b::101:101", "::101:101"],
)
def test_global_addresses_are_public(address: str) -> None:
    assert network.is_public(ipaddress.ip_address(address))


@pytest.mark.parametrize(
    "addresses",
    [["127.0.0.1"], ["10.0.0.7"], ["169.254.169.254"], ["fd00::25"], ["192.168.0.2", "::1"]],
)
async def test_internal_destinations_are_refused_like_unreachable_ones(
    monkeypatch: pytest.MonkeyPatch, addresses: list[str]
) -> None:
    answers(monkeypatch, {"imap.internal.test": addresses})

    with pytest.raises(ConnectionFailedError) as refused:
        await network.resolve("imap.internal.test", 993, STRICT, seconds=5)
    with pytest.raises(ConnectionFailedError) as unknown:
        await network.resolve("missing.internal.test", 993, STRICT, seconds=5)

    assert refused.value.code == unknown.value.code == "connection_failed"


async def test_ip_literals_are_checked_too() -> None:
    for host in ("127.0.0.1", "169.254.169.254", "::1"):
        with pytest.raises(ConnectionFailedError):
            await network.resolve(host, 5432, STRICT, seconds=5)


async def test_public_address_is_used_when_answers_are_mixed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers(monkeypatch, {"imap.example.test": ["127.0.0.1", "93.184.215.14"]})

    assert await network.resolve("imap.example.test", 993, STRICT, seconds=5) == "93.184.215.14"


async def test_allowlisted_host_name_may_resolve_to_internal_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers(monkeypatch, {"imap.lan": ["192.168.10.5"]})
    settings = MailSettings(allowed_internal_hosts=["IMAP.lan."])

    assert await network.resolve("imap.LAN", 993, settings, seconds=5) == "192.168.10.5"


async def test_allowlisted_range_covers_only_its_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers(monkeypatch, {"a.lan": ["192.168.10.5"], "b.lan": ["192.168.20.5"]})
    settings = MailSettings(allowed_internal_hosts=["192.168.10.0/24"])

    assert await network.resolve("a.lan", 993, settings, seconds=5) == "192.168.10.5"
    with pytest.raises(ConnectionFailedError):
        await network.resolve("b.lan", 993, settings, seconds=5)


async def test_imap_provider_refuses_internal_host_without_connecting() -> None:
    accepted: list[bool] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        accepted.append(True)
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        provider = ImapProvider(
            MailboxConfig(
                mailbox_id=uuid.uuid4(),
                type=MailboxType.IMAP,
                address="erika@example.org",
                settings={"host": "127.0.0.1", "port": port, "security": "none"},
                credentials={"password": "secret"},
            ),
            mail_settings=STRICT,
        )
        with pytest.raises(ConnectionFailedError) as error:
            await provider.list_folders()
    finally:
        server.close()

    assert error.value.code == "connection_failed"
    assert accepted == []


async def test_imap_connection_goes_to_the_checked_address() -> None:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"* OK [CAPABILITY IMAP4rev1] ready\r\n")
        await reader.read()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        # The name does not resolve: only ``address`` can be the target.
        conn = await ImapConnection.open(
            "imap.unresolvable.invalid",
            port,
            security="none",
            ssl_context=None,
            connect_timeout=2,
            command_timeout=2,
            address="127.0.0.1",
        )
        assert "IMAP4REV1" in conn.capabilities
        await conn.close(logout=False)
    finally:
        server.close()


async def test_smtp_connection_goes_to_the_checked_address() -> None:
    received: list[bytes] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"220 smtp.test ESMTP\r\n")
        while line := await reader.readline():
            received.append(line.strip())
            if line.upper().startswith(b"QUIT"):
                writer.write(b"221 bye\r\n")
                break
            writer.write(b"250 smtp.test\r\n")
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    target = SmtpTarget(
        host="smtp.unresolvable.invalid",
        port=port,
        security="none",
        verify_certificate=True,
        username="erika@example.org",
        auth="password",
        secret="secret",
        timeout=5,
        address="127.0.0.1",
    )

    def connect_and_quit() -> None:
        client = _open(target)
        client.quit()

    try:
        await asyncio.to_thread(connect_and_quit)
    finally:
        server.close()

    assert received[0].upper().startswith(b"EHLO")
