"""Configuration of one LDAP / Active Directory directory (everything except the bind
password, which is stored encrypted in its own column)."""

import enum
import re
from typing import Annotated, Any, Self
from urllib.parse import urlsplit

from cryptography import x509
from pydantic import AfterValidator, BaseModel, Field, StringConstraints, model_validator

from app.auth.providers.ldap.filters import normalize_dn, validate_filter_template

# Directory name in URLs and in the provider key ``ldap:<name>``.
DIRECTORY_NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,31}$"
_ATTRIBUTE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*$|^[0-9]+(\.[0-9]+)+$")


class DirectoryType(enum.StrEnum):
    ACTIVE_DIRECTORY = "active_directory"
    OPENLDAP = "openldap"


class TlsMode(enum.StrEnum):
    # TLS from the first byte (``ldaps://``, usually port 636).
    LDAPS = "ldaps"
    # ``ldap://`` upgraded with the StartTLS extended operation before any bind.
    STARTTLS = "starttls"
    # No encryption. Only allowed with OLLAMAIL_AUTH_LDAP_ALLOW_PLAINTEXT=true.
    NONE = "none"


def _dn(value: str) -> str:
    normalize_dn(value)
    return value.strip()


def _attribute(value: str) -> str:
    if not _ATTRIBUTE.match(value):
        raise ValueError("invalid attribute name")
    return value


def _group_dn(value: str) -> str:
    return normalize_dn(value)


def _user_filter(value: str) -> str:
    return validate_filter_template(value, placeholder=True)


def _plain_filter(value: str) -> str:
    return validate_filter_template(value, placeholder=False)


def _ca_certificate(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    try:
        certificates = x509.load_pem_x509_certificates(value.encode())
    except ValueError as exc:
        raise ValueError("ca_certificate must contain PEM certificates") from exc
    if not certificates:
        raise ValueError("ca_certificate must contain PEM certificates")
    return value.strip() + "\n"


def _server_url(value: str) -> str:
    parts = urlsplit(value.strip())
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("invalid port") from exc
    if parts.scheme not in {"ldap", "ldaps"} or not parts.hostname:
        raise ValueError("must be an ldap:// or ldaps:// URL with a host name")
    if parts.username or parts.password or parts.path not in {"", "/"} or parts.query:
        raise ValueError("must only contain scheme, host and port")
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return f"{parts.scheme}://{host}" + (f":{port}" if port else "")


Dn = Annotated[str, StringConstraints(min_length=1, max_length=1024), AfterValidator(_dn)]
GroupDn = Annotated[str, StringConstraints(max_length=1024), AfterValidator(_group_dn)]
Attribute = Annotated[str, StringConstraints(max_length=64), AfterValidator(_attribute)]
UserFilter = Annotated[str, StringConstraints(max_length=1024), AfterValidator(_user_filter)]
PlainFilter = Annotated[str, StringConstraints(max_length=1024), AfterValidator(_plain_filter)]
ServerUrl = Annotated[str, StringConstraints(max_length=512), AfterValidator(_server_url)]
CaCertificate = Annotated[
    str | None, StringConstraints(max_length=65536), AfterValidator(_ca_certificate)
]

# Defaults per directory type; a field the admin leaves out takes the preset's value.
PRESETS: dict[DirectoryType, dict[str, Any]] = {
    DirectoryType.ACTIVE_DIRECTORY: {
        "user_filter": (
            "(&(objectCategory=person)(objectClass=user)"
            "(|(sAMAccountName={login})(userPrincipalName={login})))"
        ),
        "subject_attribute": "objectGUID",
        "email_attribute": "mail",
        "display_name_attribute": "displayName",
        "group_filter": "(objectClass=group)",
        "group_member_attribute": "member",
    },
    DirectoryType.OPENLDAP: {
        "user_filter": "(&(objectClass=inetOrgPerson)(uid={login}))",
        "subject_attribute": "entryUUID",
        "email_attribute": "mail",
        "display_name_attribute": "cn",
        "group_filter": "(|(objectClass=groupOfNames)(objectClass=groupOfUniqueNames))",
        "group_member_attribute": "member",
    },
}


class LdapDirectorySettings(BaseModel):
    """Connection, search and mapping settings of a directory."""

    directory_type: DirectoryType = DirectoryType.ACTIVE_DIRECTORY
    # Tried in order; the next one is used if a server is unreachable (failover).
    server_urls: list[ServerUrl] = Field(min_length=1, max_length=10)
    tls_mode: TlsMode = TlsMode.LDAPS
    # PEM CA certificate(s) to trust instead of the system CAs (e.g. an internal AD CA).
    # The server certificate and host name are always verified.
    ca_certificate: CaCertificate = None
    # Service account used to search users and groups.
    bind_dn: Dn
    user_base_dn: Dn
    # Search filter with the placeholder {login}; the login is escaped (RFC 4515).
    user_filter: UserFilter
    # Stable, unique ID of a user: objectGUID (AD) or entryUUID (OpenLDAP).
    subject_attribute: Attribute
    email_attribute: Attribute
    display_name_attribute: Attribute
    # Group search; defaults to ``user_base_dn``.
    group_base_dn: Dn | None = None
    group_filter: PlainFilter
    group_member_attribute: Attribute
    # Include groups of groups. AD uses LDAP_MATCHING_RULE_IN_CHAIN, other directories
    # are searched level by level.
    nested_groups: bool = True
    # If set, only members of at least one of these groups may sign in.
    allowed_groups: list[GroupDn] = Field(default_factory=list, max_length=100)
    # Members of these groups get the role admin, everybody else the role user. If empty,
    # roles are not managed by the directory (new users get the role user).
    admin_groups: list[GroupDn] = Field(default_factory=list, max_length=100)
    # Seconds for establishing a connection (per server) and for each operation.
    connect_timeout: float = Field(default=5.0, ge=0.5, le=60)
    operation_timeout: float = Field(default=10.0, ge=0.5, le=120)

    @model_validator(mode="before")
    @classmethod
    def _apply_preset(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        try:
            kind = DirectoryType(data.get("directory_type", DirectoryType.ACTIVE_DIRECTORY))
        except ValueError:
            return data  # reported by field validation
        preset = PRESETS[kind]
        return {**preset, **{k: v for k, v in data.items() if v is not None or k not in preset}}

    @model_validator(mode="after")
    def _urls_match_tls_mode(self) -> Self:
        scheme = "ldaps" if self.tls_mode is TlsMode.LDAPS else "ldap"
        for url in self.server_urls:
            if not url.startswith(f"{scheme}://"):
                raise ValueError(f"tls_mode {self.tls_mode.value} requires {scheme}:// URLs")
        return self

    @property
    def is_active_directory(self) -> bool:
        return self.directory_type is DirectoryType.ACTIVE_DIRECTORY
