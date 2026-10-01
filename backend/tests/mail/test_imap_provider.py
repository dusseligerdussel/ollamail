"""``ImapProvider`` / ``ImapConnection`` unit tests without a real server.

A scripted server (``ScriptedServer``) checks the exact client commands for protocol paths
the test server does not cover: XOAUTH2, synchronising literals, timeouts.
"""

import asyncio
import base64
import uuid
from collections.abc import Callable
from typing import Any

import pytest

from app.core.config import MailSettings
from app.mail.models import FolderRole, MailboxType
from app.mail.providers import registry
from app.mail.providers.base import (
    AuthenticationError,
    ConfigurationError,
    ConnectionFailedError,
    MailboxConfig,
    MessageNotFoundError,
)
from app.mail.providers.imap import ImapProvider, _copyuid, _folders, make_ref, parse_ref
from app.mail.providers.imap_client import (
    CommandResult,
    ImapCommandError,
    ImapConnection,
    ImapConnectionError,
)
from app.mail.providers.imap_protocol import ResponseCode, Untagged, parse_response

SECURE = MailSettings()
INSECURE = MailSettings(allow_insecure_connections=True)


def config(**settings: Any) -> MailboxConfig:
    return MailboxConfig(
        mailbox_id=uuid.uuid4(),
        type=MailboxType.IMAP,
        address="erika@example.org",
        settings={"host": "imap.example.org", **settings},
        credentials={"password": "secret"},
    )


def test_registered_for_imap_mailboxes() -> None:
    provider = registry.create(config())
    assert isinstance(provider, ImapProvider)
    assert provider.capabilities.push and provider.capabilities.keywords


@pytest.mark.parametrize(
    "settings", [{"security": "none"}, {"security": "starttls", "verify_certificate": False}]
)
def test_insecure_connections_need_admin_flag(settings: dict[str, Any]) -> None:
    with pytest.raises(ConfigurationError) as error:
        ImapProvider(config(**settings), mail_settings=SECURE)
    assert error.value.code == "insecure_connection_refused"
    ImapProvider(config(**settings), mail_settings=INSECURE)


@pytest.mark.parametrize("settings", [{"host": ""}, {"port": 0}, {"security": "ssl3"}])
def test_invalid_settings_are_rejected(settings: dict[str, Any]) -> None:
    with pytest.raises(ConfigurationError) as error:
        ImapProvider(config(**settings), mail_settings=SECURE)
    assert error.value.code == "invalid_configuration"


def test_remote_refs() -> None:
    ref = make_ref("Projekte:2026", 17, 42)
    assert ref == "17:42:Projekte:2026"
    assert parse_ref(ref) == ("Projekte:2026", 17, 42)
    with pytest.raises(MessageNotFoundError):
        parse_ref("fake-1")


def untagged(line: bytes) -> Untagged:
    response = parse_response([line])
    assert isinstance(response, Untagged)
    return response


def test_folder_roles_from_names_when_server_has_no_special_use() -> None:
    folders = _folders(
        [
            untagged(b'* LIST (\\HasNoChildren) "." INBOX'),
            untagged(b'* LIST (\\HasNoChildren) "." "Gesendete Elemente"'),
            untagged(b'* LIST (\\HasNoChildren) "." Papierkorb'),
            untagged(b'* LIST (\\HasNoChildren) "." Spam'),
            untagged(b'* LIST (\\HasNoChildren) "." Entw&APw-rfe'),
            untagged(b'* LIST (\\HasChildren) "." Archiv'),
            untagged(b'* LIST (\\HasNoChildren) "." Archiv.Sent'),
            untagged(b'* LIST (\\Noselect \\HasChildren) "." Shared'),
        ]
    )
    roles = {f.remote_id: f.role for f in folders}
    assert roles == {
        "INBOX": FolderRole.INBOX,
        "Gesendete Elemente": FolderRole.SENT,
        "Papierkorb": FolderRole.TRASH,
        "Spam": FolderRole.JUNK,
        "Entw&APw-rfe": FolderRole.DRAFTS,
        "Archiv": FolderRole.ARCHIVE,
        # Only top-level folders are guessed.
        "Archiv.Sent": None,
    }
    sub = next(f for f in folders if f.remote_id == "Archiv.Sent")
    assert (sub.name, sub.parent_id) == ("Sent", "Archiv")


def test_special_use_wins_over_names() -> None:
    folders = _folders(
        [
            untagged(b'* LIST (\\Sent) "/" "Sent Items"'),
            untagged(b'* LIST () "/" Sent'),
        ]
    )
    assert {f.remote_id: f.role for f in folders} == {"Sent Items": FolderRole.SENT, "Sent": None}


def test_copyuid_maps_source_to_target_in_order() -> None:
    result = CommandResult(code=ResponseCode("COPYUID", ("99", "7,3:4", "10:12")))
    assert _copyuid(result, 7) == (99, 10)
    assert _copyuid(result, 4) == (99, 12)
    assert _copyuid(result, 5) is None
    assert _copyuid(CommandResult(), 7) is None


# --- Scripted server ----------------------------------------------------------------------

Script = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Any]


class ScriptedServer:
    def __init__(self, script: Script) -> None:
        self.script = script
        self.received: list[bytes] = []
        self.port = 0
        self._server: asyncio.Server | None = None

    async def __aenter__(self) -> "ScriptedServer":
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                await self.script(reader, writer)
            finally:
                writer.close()

        self._server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._server is not None
        self._server.close()


async def line(reader: asyncio.StreamReader, server: "list[bytes]") -> bytes:
    data = (await reader.readuntil(b"\r\n"))[:-2]
    server.append(data)
    return data


async def connect(port: int, **kwargs: Any) -> ImapConnection:
    return await ImapConnection.open(
        "127.0.0.1",
        port,
        security="none",
        ssl_context=None,
        connect_timeout=2,
        command_timeout=kwargs.pop("command_timeout", 2),
    )


@pytest.fixture
def received() -> list[bytes]:
    return []


async def test_xoauth2_with_sasl_ir(received: list[bytes]) -> None:
    async def script(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"* OK [CAPABILITY IMAP4rev1 AUTH=XOAUTH2 SASL-IR] ready\r\n")
        tag = (await line(reader, received)).split()[0]
        writer.write(tag + b" OK [CAPABILITY IMAP4rev1 IDLE] authenticated\r\n")
        await reader.read()

    async with ScriptedServer(script) as server:
        conn = await connect(server.port)
        await conn.authenticate_xoauth2("erika@example.org", "token-123")
        assert "IDLE" in conn.capabilities
        await conn.close(logout=False)

    payload = received[0].split()[-1]
    assert received[0].split()[1:3] == [b"AUTHENTICATE", b"XOAUTH2"]
    assert base64.b64decode(payload) == b"user=erika@example.org\x01auth=Bearer token-123\x01\x01"


async def test_xoauth2_failure_without_sasl_ir(received: list[bytes]) -> None:
    async def script(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"* OK [CAPABILITY IMAP4rev1 AUTH=XOAUTH2] ready\r\n")
        tag = (await line(reader, received)).split()[0]
        writer.write(b"+ \r\n")
        await line(reader, received)  # initial response
        writer.write(b"+ eyJzdGF0dXMiOiI0MDEifQ==\r\n")
        assert await line(reader, received) == b""  # client aborts the exchange
        writer.write(tag + b" NO [AUTHENTICATIONFAILED] invalid credentials\r\n")
        await reader.read()

    async with ScriptedServer(script) as server:
        conn = await connect(server.port)
        with pytest.raises(ImapCommandError) as error:
            await conn.authenticate_xoauth2("erika@example.org", "expired")
        assert error.value.code == "AUTHENTICATIONFAILED"
        # Errors never contain server text or credentials.
        assert "invalid credentials" not in str(error.value)
        assert "expired" not in str(error.value)
        await conn.close(logout=False)


async def test_synchronising_literal_for_non_ascii_password(received: list[bytes]) -> None:
    password = "pässwörd"

    async def script(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"* OK [CAPABILITY IMAP4rev1] ready\r\n")
        first = await line(reader, received)
        tag = first.split()[0]
        size = len(password.encode())
        assert first.endswith(b"{%d}" % size)
        writer.write(b"+ go ahead\r\n")
        rest = await reader.readexactly(size + 2)
        received.append(rest)
        writer.write(tag + b" OK [CAPABILITY IMAP4rev1] logged in\r\n")
        await reader.read()

    async with ScriptedServer(script) as server:
        conn = await connect(server.port)
        await conn.login("erika@example.org", password)
        await conn.close(logout=False)
    assert received[0].startswith(b'A0001 LOGIN "erika@example.org" {')
    assert received[1] == password.encode() + b"\r\n"


async def test_login_disabled_without_tls() -> None:
    async def script(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"* OK [CAPABILITY IMAP4rev1 STARTTLS LOGINDISABLED] ready\r\n")
        await reader.read()

    async with ScriptedServer(script) as server:
        conn = await connect(server.port)
        with pytest.raises(ImapCommandError):
            await conn.login("erika@example.org", "secret")
        await conn.close(logout=False)


async def test_timeout_closes_the_connection() -> None:
    async def script(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"* OK [CAPABILITY IMAP4rev1] ready\r\n")
        await reader.read()  # never answers

    async with ScriptedServer(script) as server:
        conn = await connect(server.port, command_timeout=0.2)
        with pytest.raises(ImapConnectionError) as error:
            await conn.command("NOOP")
        assert error.value.reason == "timeout"
        assert conn.closed


async def test_provider_maps_errors_without_server_data(received: list[bytes]) -> None:
    async def script(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"* OK [CAPABILITY IMAP4rev1 AUTH=PLAIN] ready\r\n")
        tag = (await line(reader, received)).split()[0]
        writer.write(tag + b" NO [AUTHENTICATIONFAILED] erika@example.org unknown\r\n")
        await reader.read()

    async with ScriptedServer(script) as server:
        provider = ImapProvider(
            config(host="127.0.0.1", port=server.port, security="none"), mail_settings=INSECURE
        )
        with pytest.raises(AuthenticationError) as error:
            await provider.list_folders()
        assert "example.org" not in repr(error.value)

    refused = ImapProvider(
        config(host="127.0.0.1", port=1, security="none"), mail_settings=INSECURE
    )
    with pytest.raises(ConnectionFailedError) as connection_error:
        await refused.list_folders()
    assert connection_error.value.code == "connection_failed"


async def test_xoauth2_uses_token_provider() -> None:
    tokens: list[str] = []

    async def script(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"* OK [CAPABILITY IMAP4rev1 AUTH=XOAUTH2 SASL-IR] ready\r\n")
        request = (await reader.readuntil(b"\r\n")).split()
        tokens.append(base64.b64decode(request[-1]).decode())
        writer.write(request[0] + b" OK [CAPABILITY IMAP4rev1] ok\r\n")
        request = (await reader.readuntil(b"\r\n")).split()
        writer.write(b'* LIST () "/" INBOX\r\n' + request[0] + b" OK done\r\n")
        await reader.read()

    async def token_provider(config: MailboxConfig) -> str:
        return "fresh-token"

    async with ScriptedServer(script) as server:
        provider = ImapProvider(
            config(host="127.0.0.1", port=server.port, security="none", auth="xoauth2"),
            mail_settings=INSECURE,
            token_provider=token_provider,
        )
        folders = await provider.list_folders()
        await provider.aclose()
    assert [f.remote_id for f in folders] == ["INBOX"]
    assert "auth=Bearer fresh-token" in tokens[0]
