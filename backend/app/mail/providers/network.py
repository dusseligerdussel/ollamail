"""Destination check for the mail servers users enter (IMAP and SMTP host/port).

Without it every signed-in user could make the API or the worker connect to internal
services (the database, Ollama, a cloud metadata endpoint) and tell open from closed ports
by the error code. ``resolve`` therefore resolves the host once and returns the address to
connect to. Addresses that are not globally reachable (loopback, RFC 1918, link-local
(including ``169.254.169.254``), unique local (``fc00::/7``), shared (``100.64.0.0/10``),
multicast, reserved) are refused unless ``OLLAMAIL_MAIL_ALLOWED_INTERNAL_HOSTS`` lists the
host name or a range containing the address (e.g. the IMAP server in the own LAN).

Callers connect to the returned address and keep the host name for TLS (SNI and
certificate check), so a second, different DNS answer (DNS rebinding) cannot redirect the
connection. A refused destination raises the same ``ConnectionFailedError`` as an
unreachable one. Logs carry no host names or addresses (docs/PRIVACY.md).
"""

import asyncio
import ipaddress
import socket
from dataclasses import dataclass

from app.core.config import MailSettings
from app.core.logging import get_logger
from app.mail.providers.base import ConnectionFailedError

log = get_logger(__name__)

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


@dataclass(frozen=True, slots=True)
class Allowlist:
    """``OLLAMAIL_MAIL_ALLOWED_INTERNAL_HOSTS``: host names and address ranges."""

    names: frozenset[str]
    networks: tuple[IPNetwork, ...]

    @classmethod
    def parse(cls, entries: list[str]) -> "Allowlist":
        names: set[str] = set()
        networks: list[IPNetwork] = []
        for entry in entries:
            try:
                networks.append(ipaddress.ip_network(entry, strict=False))
            except ValueError:
                names.add(_normalise(entry))
        return cls(frozenset(names), tuple(networks))

    def allows_host(self, host: str) -> bool:
        return _normalise(host) in self.names

    def allows_address(self, address: IPAddress) -> bool:
        return any(address in network for network in self.networks)


def is_public(address: IPAddress) -> bool:
    """Whether ``address`` is globally reachable (IPv4-mapped IPv6 judged as IPv4)."""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_global and not address.is_multicast


async def resolve(host: str, port: int, settings: MailSettings, *, seconds: float) -> str:
    """The address to connect to for ``host``: the first one that is public or allowed.

    Raises ``ConnectionFailedError`` if the name does not resolve within ``seconds`` or
    every address is internal and not on the allowlist.
    """
    allowlist = Allowlist.parse(settings.allowed_internal_hosts)
    try:
        async with asyncio.timeout(seconds):
            infos = await asyncio.get_running_loop().getaddrinfo(
                host, port, type=socket.SOCK_STREAM
            )
    except (OSError, UnicodeError, TimeoutError):
        raise ConnectionFailedError() from None
    trusted_name = allowlist.allows_host(host)
    for *_, sockaddr in infos:
        # IPv6 link-local answers may carry a zone ("fe80::1%eth0"); it is kept for connect.
        literal = str(sockaddr[0])
        address = ipaddress.ip_address(literal.partition("%")[0])
        if trusted_name or is_public(address) or allowlist.allows_address(address):
            return literal
    log.warning("mail_destination_refused", port=port)
    raise ConnectionFailedError()


def _normalise(host: str) -> str:
    return host.strip().rstrip(".").lower()
