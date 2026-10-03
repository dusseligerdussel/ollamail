"""Minimal asyncio IMAP4rev1 client for the commands ``ImapProvider`` needs.

One command runs at a time per connection; responses are read until the tagged result
(no background reader), so a connection is simple to reason about. ``IDLE`` runs on its own
connection (see ``ImapProvider.watch``).

Why not a library: ``aioimaplib`` lacks STARTTLS, logs raw server data (mail content) at
DEBUG level and leaves response parsing to the caller. The subset needed here is small.

Privacy: nothing in this module logs or puts server data into exceptions; errors carry
only the command name, the status and the response code (e.g. ``AUTHENTICATIONFAILED``).
"""

import asyncio
import base64
import ssl
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from app.mail.providers.imap_protocol import (
    Continuation,
    ImapParseError,
    ResponseCode,
    Tagged,
    Untagged,
    Value,
    as_int,
    literal_size,
    parse_response,
    quote,
)

Security = Literal["tls", "starttls", "none"]

# A ``SEARCH`` result for a big folder is one long line.
_LINE_LIMIT = 32 * 1024 * 1024
# Literals above this size are refused (protects the worker's memory).
MAX_LITERAL = 256 * 1024 * 1024
# Literals up to this size may use ``LITERAL-`` (RFC 7888).
_LITERAL_MINUS_MAX = 4096


class ImapError(Exception):
    """Base class; the message never contains server data or credentials."""


class ImapConnectionError(ImapError):
    """Network failure, TLS failure, timeout or the server closed the connection."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ImapProtocolError(ImapError):
    """The server sent an invalid or unexpected response."""


class ImapCommandError(ImapError):
    """A command completed with ``NO`` or ``BAD``."""

    def __init__(self, command: str, status: str, code: str | None) -> None:
        super().__init__(f"{command} failed: {status} [{code or '-'}]")
        self.command = command
        self.status = status
        self.code = code


@dataclass(frozen=True, slots=True)
class Literal_:
    """A command argument that is always sent as literal."""

    data: bytes


Arg = str | bytes | Literal_


@dataclass(slots=True)
class CommandResult:
    untagged: list[Untagged] = field(default_factory=list)
    code: ResponseCode | None = None

    def of_kind(self, kind: str) -> list[Untagged]:
        return [response for response in self.untagged if response.kind == kind]

    def codes(self) -> list[ResponseCode]:
        """Response codes of untagged status responses and of the tagged result."""
        codes = [r.code for r in self.untagged if r.code is not None]
        return [*codes, self.code] if self.code is not None else codes


@dataclass(frozen=True, slots=True)
class SelectInfo:
    uidvalidity: int
    uidnext: int | None
    exists: int
    # ``None`` without CONDSTORE or with ``[NOMODSEQ]``.
    highestmodseq: int | None
    permanent_flags: frozenset[str]
    read_only: bool


def _capabilities(values: Sequence[Value]) -> frozenset[str]:
    return frozenset(v.upper() for v in values if isinstance(v, str))


class ImapConnection:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        command_timeout: float,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._timeout = command_timeout
        self._lock = asyncio.Lock()
        self._tag = 0
        self.capabilities: frozenset[str] = frozenset()
        self.enabled: frozenset[str] = frozenset()
        self.selected: tuple[bytes, bool] | None = None
        self.closed = False

    # -- connection -------------------------------------------------------------------

    @classmethod
    async def open(
        cls,
        host: str,
        port: int,
        *,
        security: Security,
        ssl_context: ssl.SSLContext | None,
        connect_timeout: float,
        command_timeout: float,
        address: str | None = None,
    ) -> "ImapConnection":
        """``address``: connect there instead of resolving ``host`` again (checked by
        ``app.mail.providers.network``); TLS still verifies the certificate for ``host``."""
        if security != "none" and ssl_context is None:
            raise ValueError("TLS needs an SSL context")
        try:
            async with asyncio.timeout(connect_timeout):
                reader, writer = await asyncio.open_connection(
                    address or host,
                    port,
                    ssl=ssl_context if security == "tls" else None,
                    server_hostname=host if security == "tls" else None,
                    limit=_LINE_LIMIT,
                )
        except ssl.SSLCertVerificationError:
            raise ImapConnectionError("tls_certificate_invalid") from None
        except ssl.SSLError:
            raise ImapConnectionError("tls_failed") from None
        except TimeoutError:
            raise ImapConnectionError("connect_timeout") from None
        except OSError:
            raise ImapConnectionError("connect_failed") from None

        connection = cls(reader, writer, command_timeout=command_timeout)
        try:
            await connection._greeting(connect_timeout)
            if security == "starttls":
                assert ssl_context is not None
                await connection._starttls(host, ssl_context, connect_timeout)
            if not connection.capabilities:
                await connection.capability()
        except BaseException:
            await connection.close(logout=False)
            raise
        return connection

    async def _greeting(self, seconds: float) -> None:
        async with asyncio.timeout(seconds):
            response = await self._read_response()
        if not isinstance(response, Untagged) or response.kind not in {"OK", "PREAUTH"}:
            raise ImapConnectionError("server_rejected_connection")
        if response.code is not None and response.code.name == "CAPABILITY":
            self.capabilities = _capabilities(response.code.args)

    async def _starttls(self, host: str, context: ssl.SSLContext, seconds: float) -> None:
        if not self.capabilities:
            await self.capability()
        if "STARTTLS" not in self.capabilities:
            raise ImapConnectionError("starttls_unsupported")
        await self.command("STARTTLS")
        try:
            async with asyncio.timeout(seconds):
                await self._writer.start_tls(context, server_hostname=host)
        except ssl.SSLCertVerificationError:
            raise ImapConnectionError("tls_certificate_invalid") from None
        except ssl.SSLError:
            raise ImapConnectionError("tls_failed") from None
        except (TimeoutError, OSError):
            raise ImapConnectionError("tls_failed") from None
        # Capabilities before TLS must be discarded (RFC 3501 §6.2.1).
        self.capabilities = frozenset()
        await self.capability()

    async def close(self, *, logout: bool = True) -> None:
        if self.closed:
            return
        if logout:
            try:
                async with asyncio.timeout(2):
                    await self.command("LOGOUT")
            except (ImapError, TimeoutError, OSError):
                pass
        self.closed = True
        self._writer.close()
        try:
            async with asyncio.timeout(2):
                await self._writer.wait_closed()
        except (TimeoutError, OSError, ssl.SSLError):
            pass

    # -- reading ----------------------------------------------------------------------

    async def _read_line(self) -> bytes:
        try:
            line = await self._reader.readuntil(b"\r\n")
        except asyncio.IncompleteReadError:
            self.closed = True
            raise ImapConnectionError("connection_closed") from None
        except asyncio.LimitOverrunError:
            self.closed = True
            raise ImapProtocolError("line too long") from None
        except (OSError, ssl.SSLError):
            self.closed = True
            raise ImapConnectionError("connection_lost") from None
        return line[:-2]

    async def _read_response(self) -> Untagged | Tagged | Continuation:
        segments: list[bytes] = []
        while True:
            line = await self._read_line()
            segments.append(line)
            size = literal_size(line)
            if size is None:
                break
            if size > MAX_LITERAL:
                self.closed = True
                raise ImapProtocolError("literal too large")
            try:
                segments.append(await self._reader.readexactly(size))
            except asyncio.IncompleteReadError:
                self.closed = True
                raise ImapConnectionError("connection_closed") from None
            except (OSError, ssl.SSLError):
                self.closed = True
                raise ImapConnectionError("connection_lost") from None
        try:
            return parse_response(segments)
        except ImapParseError:
            raise ImapProtocolError("invalid response") from None

    async def _next(self) -> Untagged | Tagged | Continuation:
        try:
            async with asyncio.timeout(self._timeout):
                return await self._read_response()
        except TimeoutError:
            # The stream position is unknown now; the connection cannot be reused.
            self.closed = True
            self._writer.close()
            raise ImapConnectionError("timeout") from None

    # -- commands ---------------------------------------------------------------------

    def _new_tag(self) -> str:
        self._tag += 1
        return f"A{self._tag:04d}"

    async def _write(self, data: bytes) -> None:
        if self.closed:
            raise ImapConnectionError("connection_closed")
        try:
            self._writer.write(data)
            async with asyncio.timeout(self._timeout):
                await self._writer.drain()
        except (OSError, ssl.SSLError, TimeoutError):
            self.closed = True
            raise ImapConnectionError("connection_lost") from None

    async def _send(self, tag: str, parts: Sequence[Arg], result: CommandResult) -> None:
        line = bytearray(tag.encode("ascii"))
        for part in parts:
            line += b" "
            if isinstance(part, str):
                line += part.encode("ascii")
                continue
            data = part.data if isinstance(part, Literal_) else part
            quoted = None if isinstance(part, Literal_) else quote(data)
            if quoted is not None:
                line += quoted
                continue
            non_sync = "LITERAL+" in self.capabilities or (
                "LITERAL-" in self.capabilities and len(data) <= _LITERAL_MINUS_MAX
            )
            if non_sync:
                line += b"{%d+}\r\n" % len(data) + data
                continue
            line += b"{%d}\r\n" % len(data)
            await self._write(bytes(line))
            line = bytearray(data)
            await self._wait_continuation(tag, parts, result)
        line += b"\r\n"
        await self._write(bytes(line))

    async def _wait_continuation(
        self, tag: str, parts: Sequence[Arg], result: CommandResult
    ) -> None:
        while True:
            response = await self._next()
            if isinstance(response, Continuation):
                return
            if isinstance(response, Untagged):
                result.untagged.append(response)
                continue
            self._check(response, tag, parts)
            raise ImapProtocolError("command completed before literal was sent")

    def _check(self, response: Tagged, tag: str, parts: Sequence[Arg]) -> None:
        if response.tag != tag:
            raise ImapProtocolError("unexpected tag")
        if response.status != "OK":
            name = next((p for p in parts if isinstance(p, str) and p != "UID"), "?")
            code = response.code.name if response.code is not None else None
            raise ImapCommandError(name.split(" ")[0], response.status, code)

    async def _collect(self, tag: str, parts: Sequence[Arg], result: CommandResult) -> None:
        while True:
            response = await self._next()
            if isinstance(response, Untagged):
                # After ``BYE`` the server closes the connection; reading then fails.
                result.untagged.append(response)
            elif isinstance(response, Tagged):
                self._check(response, tag, parts)
                result.code = response.code
                return
            else:
                raise ImapProtocolError("unexpected continuation")

    async def command(self, *parts: Arg) -> CommandResult:
        """Run one command and return its untagged responses.

        ``str`` parts are sent verbatim (atoms, sequence sets, parenthesised lists),
        ``bytes`` as quoted string or literal, ``Literal_`` always as literal.
        """
        async with self._lock:
            tag = self._new_tag()
            result = CommandResult()
            await self._send(tag, parts, result)
            await self._collect(tag, parts, result)
            for code in result.codes():
                if code.name == "CAPABILITY":
                    self.capabilities = _capabilities(code.args)
            return result

    # -- typed helpers ----------------------------------------------------------------

    async def capability(self) -> frozenset[str]:
        result = await self.command("CAPABILITY")
        for response in result.of_kind("CAPABILITY"):
            self.capabilities = _capabilities(response.values)
        return self.capabilities

    async def login(self, username: str, password: str) -> None:
        if "LOGINDISABLED" in self.capabilities:
            raise ImapCommandError("LOGIN", "NO", "PRIVACYREQUIRED")
        result = await self.command("LOGIN", username.encode(), password.encode())
        await self._after_authentication(result)

    async def authenticate_xoauth2(self, username: str, access_token: str) -> None:
        """SASL XOAUTH2 (Gmail, Microsoft 365)."""
        if "AUTH=XOAUTH2" not in self.capabilities:
            raise ImapCommandError("AUTHENTICATE", "NO", "UNSUPPORTED")
        payload = base64.b64encode(
            f"user={username}\x01auth=Bearer {access_token}\x01\x01".encode()
        ).decode("ascii")
        async with self._lock:
            tag = self._new_tag()
            parts: tuple[str, ...] = ("AUTHENTICATE", "XOAUTH2")
            result = CommandResult()
            if "SASL-IR" in self.capabilities:
                await self._write(f"{tag} AUTHENTICATE XOAUTH2 {payload}\r\n".encode())
            else:
                await self._write(f"{tag} AUTHENTICATE XOAUTH2\r\n".encode())
                await self._wait_continuation(tag, parts, result)
                await self._write(f"{payload}\r\n".encode())
            while True:
                response = await self._next()
                if isinstance(response, Continuation):
                    # Error details (base64 JSON); an empty response ends the exchange.
                    await self._write(b"\r\n")
                elif isinstance(response, Untagged):
                    result.untagged.append(response)
                else:
                    self._check(response, tag, parts)
                    result.code = response.code
                    break
        await self._after_authentication(result)

    async def _after_authentication(self, result: CommandResult) -> None:
        if result.code is not None and result.code.name == "CAPABILITY":
            self.capabilities = _capabilities(result.code.args)
        else:
            await self.capability()

    async def enable(self, *extensions: str) -> frozenset[str]:
        result = await self.command("ENABLE", *extensions)
        enabled: set[str] = set(self.enabled)
        for response in result.of_kind("ENABLED"):
            enabled.update(_capabilities(response.values))
        self.enabled = frozenset(enabled)
        return self.enabled

    async def select(self, mailbox: bytes, *, read_only: bool) -> SelectInfo:
        self.selected = None
        result = await self.command("EXAMINE" if read_only else "SELECT", mailbox)
        uidvalidity = uidnext = highestmodseq = None
        permanent: frozenset[str] = frozenset()
        exists = 0
        for response in result.untagged:
            if response.kind == "EXISTS" and response.number is not None:
                exists = response.number
        for code in result.codes():
            argument = code.args[0] if code.args else None
            if code.name == "UIDVALIDITY":
                uidvalidity = as_int(argument)
            elif code.name == "UIDNEXT":
                uidnext = as_int(argument)
            elif code.name == "HIGHESTMODSEQ":
                highestmodseq = as_int(argument)
            elif code.name == "PERMANENTFLAGS" and isinstance(argument, list):
                permanent = frozenset(f.upper() for f in argument if isinstance(f, str))
        if any(code.name == "NOMODSEQ" for code in result.codes()):
            highestmodseq = None
        if uidvalidity is None:
            raise ImapProtocolError("missing UIDVALIDITY")
        read_only = read_only or (result.code is not None and result.code.name == "READ-ONLY")
        self.selected = (mailbox, read_only)
        return SelectInfo(
            uidvalidity=uidvalidity,
            uidnext=uidnext,
            exists=exists,
            highestmodseq=highestmodseq,
            permanent_flags=permanent,
            read_only=read_only,
        )

    async def idle(self, seconds: float) -> bool:
        """One ``IDLE`` round: wait up to ``seconds`` for the server to report a
        change in the selected mailbox. Returns ``True`` on a change, ``False`` on timeout.
        """
        if "IDLE" not in self.capabilities:
            raise ImapCommandError("IDLE", "BAD", "UNSUPPORTED")
        async with self._lock:
            tag = self._new_tag()
            parts = ("IDLE",)
            result = CommandResult()
            await self._write(f"{tag} IDLE\r\n".encode())
            await self._wait_continuation(tag, parts, result)
            changed = any(_is_change(r) for r in result.untagged)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + seconds
            while not changed:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    async with asyncio.timeout(remaining):
                        response = await self._read_response()
                except TimeoutError:
                    break
                if isinstance(response, Untagged):
                    if response.kind == "BYE":
                        self.closed = True
                        raise ImapConnectionError("server_closed")
                    changed = _is_change(response)
                elif isinstance(response, Tagged):
                    # The server ended IDLE on its own.
                    self._check(response, tag, parts)
                    return changed
            await self._write(b"DONE\r\n")
            await self._collect(tag, parts, result)
            return changed


def _is_change(response: Untagged) -> bool:
    return response.kind in {"EXISTS", "EXPUNGE", "FETCH", "VANISHED", "RECENT"}
