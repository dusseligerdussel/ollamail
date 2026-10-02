"""Defaults for common SAML identity providers.

A preset only fills in attribute names and the NameID format the IdP sends by default;
validation is the same for all. Each value can be changed per provider.
"""

import enum
from dataclasses import dataclass

NAMEID_UNSPECIFIED = "urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified"
NAMEID_EMAIL = "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress"
NAMEID_PERSISTENT = "urn:oasis:names:tc:SAML:2.0:nameid-format:persistent"
NAMEID_TRANSIENT = "urn:oasis:names:tc:SAML:2.0:nameid-format:transient"
NAMEID_FORMATS = (NAMEID_UNSPECIFIED, NAMEID_EMAIL, NAMEID_PERSISTENT, NAMEID_TRANSIENT)

_CLAIMS = "http://schemas.xmlsoap.org/ws/2005/05/identity/claims"
_MS_CLAIMS = "http://schemas.microsoft.com/ws/2008/06/identity/claims"
_MS_IDENTITY = "http://schemas.microsoft.com/identity/claims"


class SAMLPreset(enum.StrEnum):
    GENERIC = "generic"
    ENTRA = "entra"
    ADFS = "adfs"
    OKTA = "okta"
    KEYCLOAK = "keycloak"


@dataclass(frozen=True)
class PresetDefaults:
    name_id_format: str
    subject_attribute: str | None
    email_attribute: str | None
    display_name_attribute: str | None
    groups_attribute: str | None


PRESETS: dict[SAMLPreset, PresetDefaults] = {
    # Entra ID's default NameID is the UPN, which can change; the object ID cannot.
    SAMLPreset.ENTRA: PresetDefaults(
        name_id_format=NAMEID_UNSPECIFIED,
        subject_attribute=f"{_MS_IDENTITY}/objectidentifier",
        email_attribute=f"{_CLAIMS}/emailaddress",
        display_name_attribute=f"{_MS_IDENTITY}/displayname",
        groups_attribute=f"{_MS_CLAIMS}/groups",
    ),
    # AD FS: claim rules issue a persistent NameID, e-mail, name and group claims.
    SAMLPreset.ADFS: PresetDefaults(
        name_id_format=NAMEID_PERSISTENT,
        subject_attribute=None,
        email_attribute=f"{_CLAIMS}/emailaddress",
        display_name_attribute=f"{_CLAIMS}/name",
        groups_attribute="http://schemas.xmlsoap.org/claims/Group",
    ),
    SAMLPreset.OKTA: PresetDefaults(
        name_id_format=NAMEID_PERSISTENT,
        subject_attribute=None,
        email_attribute="email",
        display_name_attribute="displayName",
        groups_attribute="groups",
    ),
    SAMLPreset.KEYCLOAK: PresetDefaults(
        name_id_format=NAMEID_PERSISTENT,
        subject_attribute=None,
        email_attribute="email",
        display_name_attribute="displayName",
        groups_attribute="groups",
    ),
    SAMLPreset.GENERIC: PresetDefaults(
        name_id_format=NAMEID_PERSISTENT,
        subject_attribute=None,
        email_attribute="email",
        display_name_attribute="displayName",
        groups_attribute="groups",
    ),
}
