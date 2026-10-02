"""API schemas for SAML provider administration."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field, StringConstraints

from app.auth.providers.oidc.schemas import DisplayName, Domain, ProviderName
from app.auth.providers.saml.config import normalize_certificate, normalize_url
from app.auth.providers.saml.metadata import MAX_CERTIFICATES, MAX_METADATA_BYTES
from app.auth.providers.saml.presets import SAMLPreset

Url = Annotated[str, Field(max_length=2048), AfterValidator(normalize_url)]
EntityId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1024)]
# PEM or base64 DER; stored as base64 DER.
Certificate = Annotated[str, Field(max_length=16384), AfterValidator(normalize_certificate)]
AttributeName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)
]
MetadataXml = Annotated[str, Field(min_length=1, max_length=MAX_METADATA_BYTES)]
NameIdFormat = Literal[
    "urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified",
    "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress",
    "urn:oasis:names:tc:SAML:2.0:nameid-format:persistent",
    "urn:oasis:names:tc:SAML:2.0:nameid-format:transient",
]


class SAMLProviderCreate(BaseModel):
    """A new SAML provider. The IdP comes from ``metadata_xml`` (upload), from
    ``metadata_url`` (fetched now and on refresh) or from the three ``idp_*`` fields.

    Attribute names and ``name_id_format`` that are left out come from the preset.
    """

    name: ProviderName
    display_name: DisplayName
    preset: SAMLPreset = SAMLPreset.GENERIC
    metadata_url: Url | None = None
    # Write-only: parsed into the idp_* fields, not stored.
    metadata_xml: MetadataXml | None = None
    idp_entity_id: EntityId | None = None
    idp_sso_url: Url | None = None
    idp_certificates: list[Certificate] = Field(default=[], max_length=MAX_CERTIFICATES)
    # Entity ID of ollamail at the IdP; null for the SP metadata URL.
    sp_entity_id: EntityId | None = None
    name_id_format: NameIdFormat | None = None
    # Attribute with a stable user ID; null uses the NameID.
    subject_attribute: AttributeName | None = None
    email_attribute: AttributeName | None = None
    display_name_attribute: AttributeName | None = None
    groups_attribute: AttributeName | None = None
    # The IdP vouches for e-mail addresses (needed for allowed_domains and link_by_email).
    trust_email: bool = False
    enabled: bool = True
    auto_provision: bool = True
    link_by_email: bool = False
    allowed_domains: list[Domain] = Field(default=[], max_length=100)


class SAMLProviderUpdate(BaseModel):
    """Fields to change; omitted fields stay. ``null`` clears ``metadata_url``,
    ``sp_entity_id`` and the attribute names. A new ``metadata_xml`` or ``metadata_url``
    replaces the IdP settings."""

    display_name: DisplayName | None = None
    preset: SAMLPreset | None = None
    metadata_url: Url | None = None
    metadata_xml: MetadataXml | None = None
    idp_entity_id: EntityId | None = None
    idp_sso_url: Url | None = None
    idp_certificates: list[Certificate] | None = Field(default=None, max_length=MAX_CERTIFICATES)
    sp_entity_id: EntityId | None = None
    name_id_format: NameIdFormat | None = None
    subject_attribute: AttributeName | None = None
    email_attribute: AttributeName | None = None
    display_name_attribute: AttributeName | None = None
    groups_attribute: AttributeName | None = None
    trust_email: bool | None = None
    enabled: bool | None = None
    auto_provision: bool | None = None
    link_by_email: bool | None = None
    allowed_domains: list[Domain] | None = Field(default=None, max_length=100)


class SAMLCertificateRead(BaseModel):
    fingerprint_sha256: str
    not_valid_after: datetime


class SAMLProviderRead(BaseModel):
    name: str
    # Key in identities and sessions: "saml:<name>".
    provider: str
    display_name: str
    preset: SAMLPreset
    metadata_url: str | None
    metadata_refreshed_at: datetime | None
    idp_entity_id: str
    idp_sso_url: str
    idp_certificates: list[SAMLCertificateRead]
    # Configured entity ID (null: the SP metadata URL is used).
    sp_entity_id: str | None
    # Register these at the IdP: entity ID (identifier) and ACS URL (reply URL), or let
    # the IdP read both from the SP metadata URL.
    effective_sp_entity_id: str
    redirect_uri: str
    sp_metadata_url: str
    name_id_format: str
    subject_attribute: str | None
    email_attribute: str | None
    display_name_attribute: str | None
    groups_attribute: str | None
    trust_email: bool
    enabled: bool
    auto_provision: bool
    link_by_email: bool
    allowed_domains: list[str]
    created_at: datetime
    updated_at: datetime
