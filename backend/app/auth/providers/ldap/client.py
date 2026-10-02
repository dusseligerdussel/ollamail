"""Blocking LDAP operations (``ldap3``); callers run them in a worker thread.

Flow of a login: connect to the first reachable server (TLS required unless explicitly
disabled), bind as the service account, search the user (exactly one hit), check that
the account is not disabled, then bind as the user on a fresh connection with the found
DN and the given password. Groups are resolved with the service account.

Errors never carry directory data or passwords: ``LdapError.code`` is a fixed string.
Referrals are not followed, so credentials are only ever sent to configured servers.
"""

import math
import ssl
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from ldap3 import NO_ATTRIBUTES, NONE, SIMPLE, SUBTREE, SYNC, Connection, Server, Tls
from ldap3.core.exceptions import LDAPException, LDAPSocketOpenError

from app.auth.providers.ldap.filters import (
    member_filter,
    nested_member_filter,
    normalize_dn,
    user_filter,
)
from app.auth.providers.ldap.settings import LdapDirectorySettings, TlsMode

# userAccountControl flag ACCOUNTDISABLE (MS-ADTS 2.2.16).
UAC_ACCOUNT_DISABLE = 0x2
MAX_LOGIN_LENGTH = 256
# Upper bounds for group resolution (protects against huge or cyclic hierarchies).
MAX_GROUPS = 1000
MAX_NESTING_DEPTH = 10
_PAGE_SIZE = 500
_MEMBER_BATCH = 50
# Result codes (RFC 4511).
_INVALID_CREDENTIALS = 49


class LdapError(Exception):
    """Base class; ``code`` is safe to log and to return to admins."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class LdapUnavailableError(LdapError):
    """No configured server could be reached (network, TLS, timeout)."""


class LdapConfigError(LdapError):
    """The directory answered, but the configuration does not work (e.g. the service
    account is rejected or a search fails)."""


@dataclass(frozen=True)
class LdapUser:
    dn: str
    subject: str
    email: str | None
    display_name: str | None
    # Normalised DNs (``normalize_dn``) of the user's groups.
    groups: frozenset[str]
    disabled: bool


@dataclass(frozen=True)
class ServerCheck:
    url: str
    ok: bool
    # Error code if not ok: unreachable, tls_failed, service_bind_failed, ...
    error: str | None
    latency_ms: int


def valid_credentials_input(login: str, password: str) -> bool:
    """Reject input that must never reach the directory.

    An empty password would be an *unauthenticated bind* (RFC 4513 5.1.2), which many
    servers answer with success. A NUL byte could truncate the password to empty in C
    libraries. Control characters have no place in a login name.
    """
    if not password or "\x00" in password:
        return False
    if not login or len(login) > MAX_LOGIN_LENGTH:
        return False
    return not any(ord(char) < 0x20 or ord(char) == 0x7F for char in login)


class _VerifiedTls(Tls):
    """TLS with an ``SSLContext`` that checks the certificate chain *and* host name
    during the handshake (ldap3's own check is skipped on some code paths)."""

    def __init__(self, context: ssl.SSLContext) -> None:
        super().__init__(validate=ssl.CERT_REQUIRED)
        self._context = context

    def wrap_socket(self, connection: Any, do_handshake: bool = False) -> None:
        connection.socket = self._context.wrap_socket(
            connection.socket,
            server_side=False,
            do_handshake_on_connect=do_handshake,
            server_hostname=connection.server.host,
        )


def _ssl_context(ca_certificate: str | None) -> ssl.SSLContext:
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cadata=ca_certificate)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def _first(attributes: dict[str, list[bytes]], name: str) -> bytes | None:
    for key, values in attributes.items():
        if key.lower() == name.lower() and values:
            return values[0]
    return None


def _text(value: bytes | None) -> str | None:
    if value is None:
        return None
    with suppress(UnicodeDecodeError):
        text = value.decode("utf-8").strip()
        return text or None
    return None


def format_subject(attribute: str, value: bytes) -> str:
    """Stable string form of the subject attribute.

    ``objectGUID`` is a 16-byte little-endian GUID and becomes its canonical UUID string
    (as shown by AD tools). Text values are used as they are, other binary values as hex.
    """
    if attribute.lower() == "objectguid" and len(value) == 16:
        return str(uuid.UUID(bytes_le=value))
    text = _text(value)
    if text is not None and text.isprintable():
        return text
    return value.hex()


class LdapDirectoryClient:
    def __init__(self, settings: LdapDirectorySettings, bind_password: str) -> None:
        self.settings = settings
        self._bind_password = bind_password
        self._tls = (
            None
            if settings.tls_mode is TlsMode.NONE
            else _VerifiedTls(_ssl_context(settings.ca_certificate))
        )

    # -- connections -------------------------------------------------------------------

    def _server(self, url: str) -> Server:
        parts = urlsplit(url)
        use_ssl = parts.scheme == "ldaps"
        return Server(
            parts.hostname or "",
            port=parts.port or (636 if use_ssl else 389),
            use_ssl=use_ssl,
            tls=self._tls,
            get_info=NONE,
            connect_timeout=self.settings.connect_timeout,
        )

    def _connection(self, server: Server, user: str, password: str) -> Connection:
        return Connection(
            server,
            user=user,
            password=password,
            authentication=SIMPLE,
            client_strategy=SYNC,
            auto_bind=False,
            auto_referrals=False,
            read_only=True,
            raise_exceptions=False,
            # ldap3 packs this into a struct; it must be an int.
            receive_timeout=math.ceil(self.settings.operation_timeout),
            return_empty_attributes=False,
        )

    def _open(self, server: Server, user: str, password: str) -> Connection:
        """An open (TLS-protected) connection, bound as ``user``.

        Raises ``LdapUnavailableError`` for network/TLS problems and ``LdapError`` with
        code ``invalid_credentials`` if the server rejects the credentials.
        """
        if not password:
            # Never send an unauthenticated bind (empty password).
            raise LdapError("invalid_credentials")
        connection = self._connection(server, user, password)
        try:
            connection.open()
        except LDAPSocketOpenError as exc:
            raise LdapUnavailableError(
                "tls_failed" if _is_tls_error(exc) else "unreachable"
            ) from None
        except Exception:  # ldap3 also lets socket and struct errors through
            raise LdapUnavailableError("unreachable") from None
        try:
            if self.settings.tls_mode is TlsMode.STARTTLS and not connection.start_tls():
                raise LdapUnavailableError("starttls_failed")
            if not connection.bind():
                result = connection.result or {}
                if result.get("result") == _INVALID_CREDENTIALS:
                    raise LdapError("invalid_credentials")
                raise LdapConfigError("bind_failed")
        except LdapError:
            _close(connection)
            raise
        except Exception as exc:
            _close(connection)
            raise LdapUnavailableError(
                "tls_failed" if _is_tls_error(exc) else "connection_lost"
            ) from None
        return connection

    @contextmanager
    def _service(self) -> Iterator[tuple[Server, Connection]]:
        """Service-account connection to the first working server (failover)."""
        error: LdapUnavailableError | None = None
        for url in self.settings.server_urls:
            server = self._server(url)
            try:
                connection = self._open(server, self.settings.bind_dn, self._bind_password)
            except LdapUnavailableError as exc:
                error = exc
                continue
            except LdapError as exc:
                if exc.code == "invalid_credentials":
                    raise LdapConfigError("service_bind_failed") from None
                raise
            try:
                yield server, connection
            finally:
                _close(connection)
            return
        raise error or LdapUnavailableError("unreachable")

    # -- searches ----------------------------------------------------------------------

    def _search(
        self,
        connection: Connection,
        base: str,
        search_filter: str,
        attributes: list[str] | str,
        *,
        size_limit: int = 0,
    ) -> list[dict[str, Any]]:
        """Entries (``searchResEntry``) of a paged search; referrals are dropped."""
        entries: list[dict[str, Any]] = []
        cookie: bytes | None = None
        while True:
            try:
                connection.search(
                    base,
                    search_filter,
                    search_scope=SUBTREE,
                    attributes=attributes,
                    size_limit=size_limit,
                    paged_size=_PAGE_SIZE,
                    paged_cookie=cookie,
                    time_limit=int(self.settings.operation_timeout),
                )
            except LDAPException:
                raise LdapUnavailableError("connection_lost") from None
            result = connection.result or {}
            # 0 success, 4 sizeLimitExceeded, 32 noSuchObject (empty result).
            if result.get("result") not in (0, 4, 32):
                raise LdapConfigError("search_failed")
            response = connection.response or []
            entries.extend(entry for entry in response if entry.get("type") == "searchResEntry")
            controls = result.get("controls") or {}
            cookie = controls.get("1.2.840.113556.1.4.319", {}).get("value", {}).get("cookie")
            if not cookie or (size_limit and len(entries) >= size_limit):
                return entries

    def _find(self, connection: Connection, login: str) -> LdapUser | None:
        s = self.settings
        # userAccountControl exists in AD and in OpenLDAP with the msuser schema.
        attributes = [
            s.subject_attribute,
            s.email_attribute,
            s.display_name_attribute,
            "userAccountControl",
        ]
        # Two hits are enough to detect an ambiguous filter.
        entries = self._search(
            connection, s.user_base_dn, user_filter(s.user_filter, login), attributes, size_limit=2
        )
        if len(entries) != 1:
            return None
        entry = entries[0]
        raw: dict[str, list[bytes]] = entry.get("raw_attributes") or {}
        subject = _first(raw, s.subject_attribute)
        if not subject:
            raise LdapConfigError("subject_attribute_missing")
        uac = _text(_first(raw, "userAccountControl"))
        # A value that is not a number counts as disabled (fail closed).
        disabled = uac is not None and (
            not uac.lstrip("-").isdigit() or bool(int(uac) & UAC_ACCOUNT_DISABLE)
        )
        dn: str = entry["dn"]
        return LdapUser(
            dn=dn,
            subject=format_subject(s.subject_attribute, subject),
            email=_text(_first(raw, s.email_attribute)),
            display_name=_text(_first(raw, s.display_name_attribute)),
            groups=frozenset(self._groups(connection, dn)),
            disabled=disabled,
        )

    def _groups(self, connection: Connection, user_dn: str) -> set[str]:
        s = self.settings
        base = s.group_base_dn or s.user_base_dn
        if s.nested_groups and s.is_active_directory:
            search_filter = nested_member_filter(s.group_filter, s.group_member_attribute, user_dn)
            entries = self._search(
                connection, base, search_filter, NO_ATTRIBUTES, size_limit=MAX_GROUPS
            )
            return {_normalized(entry["dn"]) for entry in entries}
        found: dict[str, str] = {}
        level = [user_dn]
        for _ in range(MAX_NESTING_DEPTH if s.nested_groups else 1):
            next_level: list[str] = []
            for start in range(0, len(level), _MEMBER_BATCH):
                batch = level[start : start + _MEMBER_BATCH]
                search_filter = member_filter(s.group_filter, s.group_member_attribute, batch)
                for entry in self._search(connection, base, search_filter, NO_ATTRIBUTES):
                    key = _normalized(entry["dn"])
                    if key not in found and len(found) < MAX_GROUPS:
                        found[key] = entry["dn"]
                        next_level.append(entry["dn"])
            if not next_level:
                break
            level = next_level
        return set(found)

    # -- public API --------------------------------------------------------------------

    def find_user(self, login: str) -> LdapUser | None:
        """Look up a user with the service account (no password check)."""
        if not login or len(login) > MAX_LOGIN_LENGTH:
            return None
        with self._service() as (_, connection):
            return self._find(connection, login)

    def authenticate(self, login: str, password: str) -> LdapUser | None:
        """The user if ``password`` is correct and the account is enabled, else ``None``.

        Unknown and disabled accounts still cost one bind (with a DN that does not
        exist), so the response time does not reveal whether an account exists.
        """
        if not valid_credentials_input(login, password):
            return None
        with self._service() as (server, connection):
            user = self._find(connection, login)
        bind_dn = user.dn if user is not None and not user.disabled else self._dummy_dn()
        try:
            _close(self._open(server, bind_dn, password))
        except LdapError as exc:
            if isinstance(exc, LdapUnavailableError):
                raise
            return None
        if user is None or user.disabled:
            return None
        return user

    def _dummy_dn(self) -> str:
        return f"cn=ollamail-nonexistent-{uuid.uuid4().hex},{self.settings.user_base_dn}"

    def check_servers(self) -> list[ServerCheck]:
        """Connect, negotiate TLS and bind as the service account on every server."""
        checks = []
        for url in self.settings.server_urls:
            started = time.monotonic()
            error: str | None = None
            try:
                _close(self._open(self._server(url), self.settings.bind_dn, self._bind_password))
            except LdapError as exc:
                error = "service_bind_failed" if exc.code == "invalid_credentials" else exc.code
            latency = int((time.monotonic() - started) * 1000)
            checks.append(ServerCheck(url=url, ok=error is None, error=error, latency_ms=latency))
        return checks


def _normalized(dn: str) -> str:
    try:
        return normalize_dn(dn)
    except ValueError:
        return dn.lower()


def _close(connection: Connection) -> None:
    with suppress(Exception):
        connection.unbind()


def _is_tls_error(exc: BaseException) -> bool:
    text = repr(exc).lower()
    return any(word in text for word in ("ssl", "certificate", "tls"))


__all__ = [
    "LdapConfigError",
    "LdapDirectoryClient",
    "LdapError",
    "LdapUnavailableError",
    "LdapUser",
    "ServerCheck",
]
