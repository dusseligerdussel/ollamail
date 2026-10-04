"""``httpx`` transport that only connects to checked destinations (``app.core.network``).

Every TCP connection, also the one after a redirect, resolves the host once, refuses
internal addresses that are not on the allowlist and connects to the checked address. The
URL is left alone, so the ``Host`` header, SNI and the certificate check keep the host name
and a second DNS answer (DNS rebinding) cannot redirect the connection. A refused
destination fails like an unreachable one (``httpx.ConnectError``).

Environment proxies are not used (a client with an explicit transport ignores them): they
would connect instead of us and skip the check. ``SSL_CERT_FILE``/``SSL_CERT_DIR`` still
apply, e.g. for a server with a private CA.
"""

import ssl
import typing

import httpcore
import httpx

from app.core import network
from app.core.logging import get_logger

log = get_logger(__name__)

# DNS lookup limit when httpx passes no connect timeout.
DEFAULT_RESOLVE_SECONDS = 10.0


class GuardedBackend(httpcore.AsyncNetworkBackend):
    """Network backend of ``httpcore`` that checks each destination before connecting."""

    def __init__(
        self,
        allowlist: network.Allowlist,
        *,
        log_event: str,
        inner: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self._allowlist = allowlist
        self._log_event = log_event
        self._inner = inner or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 (httpcore interface)
        local_address: str | None = None,
        socket_options: typing.Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            address = await network.resolve(
                host, port, self._allowlist, seconds=timeout or DEFAULT_RESOLVE_SECONDS
            )
        except network.DestinationError as exc:
            if exc.refused:
                # No host name or address in the log (docs/PRIVACY.md).
                log.warning(self._log_event, port=port)
            raise httpcore.ConnectError() from None
        return await self._inner.connect_tcp(
            address,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109 (httpcore interface)
        socket_options: typing.Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError()

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class GuardedTransport(httpx.AsyncHTTPTransport):
    """``httpx.AsyncHTTPTransport`` whose connections go through ``GuardedBackend``."""

    def __init__(
        self,
        allowed_internal_hosts: typing.Iterable[str],
        *,
        log_event: str,
        verify: ssl.SSLContext | bool = True,
        backend: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        super().__init__(verify=verify)
        # httpx has no parameter for the network backend: the pool is replaced with one
        # that has the same settings (no proxy, httpx's default limits).
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(verify=verify),
            max_connections=100,
            max_keepalive_connections=20,
            keepalive_expiry=5.0,
            network_backend=GuardedBackend(
                network.Allowlist.parse(allowed_internal_hosts),
                log_event=log_event,
                inner=backend,
            ),
        )
