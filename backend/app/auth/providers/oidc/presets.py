"""Presets for common identity providers: defaults, hints and provider-specific checks.

A preset never weakens validation; it only adds checks the generic OIDC rules miss:

* **Microsoft Entra ID**: the issuer contains the tenant. Single-tenant apps use the tenant
  ID (GUID); the ``tid`` claim must match it. Multi-tenant apps use ``organizations`` or
  ``common``; their discovery document has the issuer template ``{tenantid}``, so the token
  issuer is checked against the template filled with ``tid``, and ``tid`` must be on the
  ``allowed_tenants`` list. Without that list any Entra tenant in the world could sign in.
* **Google Workspace**: the ``hd`` claim (hosted domain) must be on ``hosted_domains``.
  The ``email`` domain alone is not enough: consumer Google accounts can use any address.
* **Keycloak**, **Authentik**: generic OIDC; only defaults and hints differ.
"""

import enum
import re
from dataclasses import dataclass
from urllib.parse import urlsplit


class OIDCPreset(enum.StrEnum):
    GENERIC = "generic"
    ENTRA = "entra"
    GOOGLE = "google"
    KEYCLOAK = "keycloak"
    AUTHENTIK = "authentik"


@dataclass(frozen=True)
class PresetInfo:
    preset: OIDCPreset
    label: str
    # Issuer with placeholders in braces, e.g. ``https://login.microsoftonline.com/{tenant}/v2.0``.
    issuer_template: str
    scopes: tuple[str, ...]
    groups_claim: str | None
    # Preset-specific settings the admin UI should show.
    fields: tuple[str, ...]
    docs: str


PRESETS: dict[OIDCPreset, PresetInfo] = {
    OIDCPreset.ENTRA: PresetInfo(
        preset=OIDCPreset.ENTRA,
        label="Microsoft Entra ID",
        issuer_template="https://login.microsoftonline.com/{tenant}/v2.0",
        scopes=("openid", "email", "profile"),
        groups_claim="groups",
        fields=("allowed_tenants",),
        docs="docs/auth/oidc.md#microsoft-entra-id",
    ),
    OIDCPreset.GOOGLE: PresetInfo(
        preset=OIDCPreset.GOOGLE,
        label="Google Workspace",
        issuer_template="https://accounts.google.com",
        scopes=("openid", "email", "profile"),
        # Google ID tokens carry no groups.
        groups_claim=None,
        fields=("hosted_domains",),
        docs="docs/auth/oidc.md#google-workspace",
    ),
    OIDCPreset.KEYCLOAK: PresetInfo(
        preset=OIDCPreset.KEYCLOAK,
        label="Keycloak",
        issuer_template="https://{host}/realms/{realm}",
        scopes=("openid", "email", "profile"),
        groups_claim="groups",
        fields=(),
        docs="docs/auth/oidc.md#keycloak",
    ),
    OIDCPreset.AUTHENTIK: PresetInfo(
        preset=OIDCPreset.AUTHENTIK,
        label="Authentik",
        issuer_template="https://{host}/application/o/{slug}/",
        scopes=("openid", "email", "profile"),
        groups_claim="groups",
        fields=(),
        docs="docs/auth/oidc.md#authentik",
    ),
    OIDCPreset.GENERIC: PresetInfo(
        preset=OIDCPreset.GENERIC,
        label="OpenID Connect",
        issuer_template="https://{host}/",
        scopes=("openid", "email", "profile"),
        groups_claim="groups",
        fields=(),
        docs="docs/auth/oidc.md#generic",
    ),
}

ENTRA_HOST = "login.microsoftonline.com"
# Tenant segments of multi-tenant Entra issuers. "consumers" (personal accounts only) is
# not supported: the instance is meant for organisations.
ENTRA_MULTI_TENANT = frozenset({"organizations", "common"})
ENTRA_TENANT_PLACEHOLDER = "{tenantid}"
GOOGLE_ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})

_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_ENTRA_ISSUER = re.compile(r"^https://login\.microsoftonline\.com/([^/]+)/v2\.0$")


def entra_tenant(issuer: str) -> str | None:
    """Tenant segment of an Entra v2.0 issuer (lower case), ``None`` if not one."""
    match = _ENTRA_ISSUER.match(issuer)
    return match.group(1).lower() if match else None


def is_guid(value: str) -> bool:
    return bool(_GUID.match(value.lower()))


def validate_issuer(issuer: str, *, allow_http: bool) -> None:
    """Raise ``ValueError`` unless ``issuer`` is an absolute https URL without query."""
    parts = urlsplit(issuer)
    if parts.scheme not in ({"https", "http"} if allow_http else {"https"}):
        raise ValueError("issuer must be an https:// URL")
    if not parts.netloc or parts.query or parts.fragment or parts.username:
        raise ValueError("issuer must be a plain URL without credentials, query or fragment")


def validate_preset(
    preset: OIDCPreset,
    issuer: str,
    allowed_tenants: list[str],
    hosted_domains: list[str],
) -> None:
    """Preset-specific configuration rules; raises ``ValueError`` with a reason."""
    if preset is OIDCPreset.ENTRA:
        tenant = entra_tenant(issuer)
        if tenant is None:
            raise ValueError(
                "Entra ID issuer must be https://login.microsoftonline.com/<tenant>/v2.0"
            )
        if tenant in ENTRA_MULTI_TENANT:
            if not allowed_tenants:
                raise ValueError("multi-tenant Entra ID requires allowed_tenants")
        elif not is_guid(tenant):
            raise ValueError("Entra ID tenant must be a tenant ID (GUID), organizations or common")
        if any(not is_guid(t) for t in allowed_tenants):
            raise ValueError("allowed_tenants must contain tenant IDs (GUIDs)")
    elif allowed_tenants:
        raise ValueError("allowed_tenants is only supported by the Entra ID preset")
    if preset is OIDCPreset.GOOGLE:
        if issuer != "https://accounts.google.com":
            raise ValueError("Google issuer must be https://accounts.google.com")
    elif hosted_domains:
        raise ValueError("hosted_domains is only supported by the Google preset")
