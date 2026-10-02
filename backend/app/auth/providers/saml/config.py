"""Validated provider configuration and the settings for python3-saml."""

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from cryptography import x509
from onelogin.saml2.constants import OneLogin_Saml2_Constants as Constants

from app.auth.providers.saml.models import SAMLProviderRecord
from app.auth.providers.saml.presets import SAMLPreset
from app.auth.provisioning import ProvisioningPolicy

PROVIDER_PREFIX = "saml:"
# One Assertion Consumer Service for all SAML providers: the encrypted flow cookie names
# the provider. A fixed path can be exempted from the CSRF check (the IdP posts the
# response cross-site, without the CSRF header).
ACS_PATH = "/auth/saml/acs"
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})
_PEM = re.compile(r"-----(BEGIN|END) CERTIFICATE-----|\s")


def login_path(name: str) -> str:
    return f"/auth/saml/{name}/login"


def metadata_path(name: str) -> str:
    return f"/auth/saml/{name}/metadata"


def normalize_url(value: str) -> str:
    """An ``https`` URL (``http`` only for localhost) without credentials or fragment.

    Raises ``ValueError`` with a static message otherwise.
    """
    value = value.strip()
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise ValueError("must be an https URL") from None
    secure = parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in _LOOPBACK)
    if (
        not secure
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
        or (port is not None and not 0 < port < 65536)
    ):
        raise ValueError("must be an https URL")
    return value


def normalize_certificate(value: str) -> str:
    """The certificate as single-line base64 DER; accepts PEM or bare base64.

    Raises ``ValueError`` if it is not an X.509 certificate.
    """
    try:
        der = base64.b64decode(_PEM.sub("", value), validate=True)
        x509.load_der_x509_certificate(der)
    except (binascii.Error, ValueError):
        raise ValueError("invalid X.509 certificate") from None
    return base64.b64encode(der).decode()


@dataclass(frozen=True)
class CertificateInfo:
    fingerprint_sha256: str
    not_valid_after: datetime


def certificate_info(value: str) -> CertificateInfo:
    der = base64.b64decode(value)
    cert = x509.load_der_x509_certificate(der)
    return CertificateInfo(
        fingerprint_sha256=hashlib.sha256(der).hexdigest(),
        not_valid_after=cert.not_valid_after_utc,
    )


@dataclass(frozen=True)
class SAMLConfig:
    name: str
    display_name: str
    idp_entity_id: str
    idp_sso_url: str
    idp_certificates: tuple[str, ...]
    name_id_format: str
    preset: SAMLPreset = SAMLPreset.GENERIC
    sp_entity_id: str | None = None
    subject_attribute: str | None = None
    email_attribute: str | None = None
    display_name_attribute: str | None = None
    groups_attribute: str | None = None
    trust_email: bool = False
    enabled: bool = True
    auto_provision: bool = True
    link_by_email: bool = False
    allowed_domains: frozenset[str] = field(default_factory=frozenset)

    @property
    def provider_name(self) -> str:
        return PROVIDER_PREFIX + self.name

    @property
    def policy(self) -> ProvisioningPolicy:
        return ProvisioningPolicy(
            auto_provision=self.auto_provision,
            link_by_email=self.link_by_email,
            allowed_domains=self.allowed_domains,
        )

    def library_settings(self, *, sp_entity_id: str, acs_url: str) -> dict[str, Any]:
        """Settings for ``OneLogin_Saml2_Settings`` (strict mode, no SP keys).

        Responses must carry a valid signature on the response or the assertion (the
        library rejects unsigned ones); SHA-1 signatures and digests are rejected. No
        authentication context is requested, so the IdP may use any method (Kerberos,
        MFA). ollamail does not sign requests and cannot decrypt assertions.
        """
        certs = list(self.idp_certificates)
        return {
            "strict": True,
            "debug": False,
            "sp": {
                "entityId": sp_entity_id,
                "assertionConsumerService": {
                    "url": acs_url,
                    "binding": Constants.BINDING_HTTP_POST,
                },
                "NameIDFormat": self.name_id_format,
                "x509cert": "",
                "privateKey": "",
            },
            "idp": {
                "entityId": self.idp_entity_id,
                "singleSignOnService": {
                    "url": self.idp_sso_url,
                    "binding": Constants.BINDING_HTTP_REDIRECT,
                },
                "x509cert": certs[0] if certs else "",
                "x509certMulti": {"signing": certs},
            },
            "security": {
                "authnRequestsSigned": False,
                "logoutRequestSigned": False,
                "logoutResponseSigned": False,
                "signMetadata": False,
                "wantMessagesSigned": False,
                "wantAssertionsSigned": False,
                "wantAssertionsEncrypted": False,
                "wantNameIdEncrypted": False,
                "wantNameId": self.subject_attribute is None,
                "wantAttributeStatement": False,
                "requestedAuthnContext": False,
                "rejectDeprecatedAlgorithm": True,
                "rejectUnsolicitedResponsesWithInResponseTo": True,
                "allowRepeatAttributeName": True,
                "signatureAlgorithm": Constants.RSA_SHA256,
                "digestAlgorithm": Constants.SHA256,
            },
        }


def from_record(record: SAMLProviderRecord) -> SAMLConfig:
    try:
        preset = SAMLPreset(record.preset)
    except ValueError:
        preset = SAMLPreset.GENERIC
    return SAMLConfig(
        name=record.name,
        display_name=record.display_name,
        preset=preset,
        idp_entity_id=record.idp_entity_id,
        idp_sso_url=record.idp_sso_url,
        idp_certificates=tuple(record.idp_certificates),
        sp_entity_id=record.sp_entity_id,
        name_id_format=record.name_id_format,
        subject_attribute=record.subject_attribute,
        email_attribute=record.email_attribute,
        display_name_attribute=record.display_name_attribute,
        groups_attribute=record.groups_attribute,
        trust_email=record.trust_email,
        enabled=record.enabled,
        auto_provision=record.auto_provision,
        link_by_email=record.link_by_email,
        allowed_domains=frozenset(record.allowed_domains),
    )
