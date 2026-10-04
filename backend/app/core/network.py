"""Destination check for servers users enter (mail servers, CalDAV servers).

Without it every signed-in user could make the API or the worker connect to internal
services (the database, Ollama, a cloud metadata endpoint) and tell open from closed ports
by the error code. ``resolve`` therefore resolves the host once and returns the address to
connect to. Addresses that are not globally reachable (loopback, RFC 1918, link-local
(including ``169.254.169.254``), unique local (``fc00::/7``), shared (``100.64.0.0/10``),
multicast, reserved) are refused unless the feature's allowlist (e.g.
``OLLAMAIL_MAIL_ALLOWED_INTERNAL_HOSTS``) lists the host name or a range containing the
address (e.g. a server in the own LAN).

Callers connect to the returned address and keep the host name for TLS (SNI and
certificate check), so a second, different DNS answer (DNS rebinding) cannot redirect the
connection. Callers report a refused destination like an unreachable one. Logs carry no
host names or addresses (docs/PRIVACY.md).
"""

import asyncio
import ipaddress
import socket
from collections.abc import Iterable
from dataclasses import dataclass

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


class DestinationError(Exception):
    """The host does not resolve in time, or only to refused addresses (``refused``)."""

    def __init__(self, *, refused: bool) -> None:
        super().__init__("destination refused" if refused else "destination unresolved")
        self.refused = refused


@dataclass(frozen=True, slots=True)
class Allowlist:
    """An ``*_ALLOWED_INTERNAL_HOSTS`` setting: host names and address ranges."""

    names: frozenset[str]
    networks: tuple[IPNetwork, ...]

    @classmethod
    def parse(cls, entries: Iterable[str]) -> "Allowlist":
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


def parse_allowed_hosts(value: list[str]) -> list[str]:
    """Settings validator: normalised entries; raises ``ValueError`` for invalid ones."""
    entries = []
    for entry in value:
        entry = _normalise(entry)
        try:
            ipaddress.ip_network(entry, strict=False)
        except ValueError:
            if not entry or "/" in entry or any(c.isspace() for c in entry):
                raise ValueError(f"not a host name, IP address or CIDR range: {entry!r}") from None
        entries.append(entry)
    return entries


def is_public(address: IPAddress) -> bool:
    """Whether ``address`` is globally reachable (IPv4-mapped IPv6 judged as IPv4)."""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_global and not address.is_multicast


async def resolve(host: str, port: int, allowlist: Allowlist, *, seconds: float) -> str:
    """The address to connect to for ``host``: the first one that is public or allowed.

    Raises ``DestinationError`` if the name does not resolve within ``seconds`` or every
    address is internal and not on the allowlist.
    """
    try:
        async with asyncio.timeout(seconds):
            infos = await asyncio.get_running_loop().getaddrinfo(
                host, port, type=socket.SOCK_STREAM
            )
    except (OSError, UnicodeError, TimeoutError):
        raise DestinationError(refused=False) from None
    trusted_name = allowlist.allows_host(host)
    for *_, sockaddr in infos:
        # IPv6 link-local answers may carry a zone ("fe80::1%eth0"); it is kept for connect.
        literal = str(sockaddr[0])
        address = ipaddress.ip_address(literal.partition("%")[0])
        if trusted_name or is_public(address) or allowlist.allows_address(address):
            return literal
    raise DestinationError(refused=True)


def _normalise(host: str) -> str:
    return host.strip().rstrip(".").lower()
