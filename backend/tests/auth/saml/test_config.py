"""Unit tests: URL and certificate validation, metadata parsing, library settings."""

import pytest
from onelogin.saml2.settings import OneLogin_Saml2_Settings
from onelogin.saml2.utils import OneLogin_Saml2_Utils

from app.auth.providers.saml.config import (
    SAMLConfig,
    certificate_info,
    normalize_certificate,
    normalize_url,
)
from app.auth.providers.saml.errors import SAMLMetadataError
from app.auth.providers.saml.metadata import MAX_METADATA_BYTES, parse_metadata
from app.auth.providers.saml.presets import PRESETS, SAMLPreset
from app.auth.providers.saml.provider import request_data, request_id
from tests.auth.saml.idp import IDP_ENTITY_ID, IDP_SSO_URL, FakeIdP, make_key_pair


@pytest.mark.parametrize(
    "url",
    [
        "https://idp.example.org/saml",
        "https://idp.example.org:8443/adfs/ls/?x=1",
        "http://localhost:8180/realms/test/protocol/saml",
        "http://127.0.0.1/sso",
    ],
)
def test_accepted_urls(url: str) -> None:
    assert normalize_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://idp.example.org/saml",
        "ftp://idp.example.org/",
        "https://user:pw@idp.example.org/",
        "https://idp.example.org/#fragment",
        "javascript:alert(1)",
        "https://",
        "https://idp.example.org:99999/",
    ],
)
def test_rejected_urls(url: str) -> None:
    with pytest.raises(ValueError):
        normalize_url(url)


def test_certificate_accepts_pem_and_base64() -> None:
    keys = make_key_pair()

    assert normalize_certificate(keys.cert_pem) == keys.cert_b64
    assert normalize_certificate(f"  {keys.cert_b64[:40]}\n{keys.cert_b64[40:]} ") == keys.cert_b64
    info = certificate_info(keys.cert_b64)
    assert len(info.fingerprint_sha256) == 64


@pytest.mark.parametrize("value", ["", "not base64!", "bm90IGEgY2VydGlmaWNhdGU="])
def test_certificate_rejects_garbage(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_certificate(value)


def test_parse_metadata() -> None:
    idp = FakeIdP()

    metadata = parse_metadata(idp.metadata())

    assert metadata.entity_id == IDP_ENTITY_ID
    assert metadata.sso_url == IDP_SSO_URL
    assert metadata.certificates == (idp.keys.cert_b64,)


def test_parse_metadata_keeps_all_signing_certificates() -> None:
    idp = FakeIdP()
    second = make_key_pair("Next")
    xml = idp.metadata().replace(
        "</md:KeyDescriptor>",
        '</md:KeyDescriptor><md:KeyDescriptor use="signing"><ds:KeyInfo><ds:X509Data>'
        f"<ds:X509Certificate>{second.cert_b64}</ds:X509Certificate>"
        "</ds:X509Data></ds:KeyInfo></md:KeyDescriptor>",
        1,
    )

    assert parse_metadata(xml).certificates == (idp.keys.cert_b64, second.cert_b64)


@pytest.mark.parametrize(
    "xml",
    [
        '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><x>&e;</x>',
        '<!DOCTYPE x [<!ENTITY a "a"><!ENTITY b "&a;&a;&a;&a;">]><x>&b;</x>',
        "<x>",
        "x" * (MAX_METADATA_BYTES + 1),
    ],
)
def test_parse_metadata_rejects_unsafe_or_invalid_xml(xml: str) -> None:
    with pytest.raises(SAMLMetadataError):
        parse_metadata(xml)


def test_parse_metadata_error_does_not_echo_the_document() -> None:
    with pytest.raises(SAMLMetadataError) as exc:
        parse_metadata("<secret-content>")

    assert "secret" not in str(exc.value)


@pytest.mark.parametrize(
    "acs_url",
    [
        "https://mail.example.org/api/auth/saml/acs",
        "https://mail.example.org:8443/api/auth/saml/acs",
        "http://localhost:8080/api/auth/saml/acs",
    ],
)
def test_request_data_reproduces_the_acs_url(acs_url: str) -> None:
    """python3-saml compares Destination/Recipient against the URL built from these."""
    assert OneLogin_Saml2_Utils.get_self_url_no_query(request_data(acs_url)) == acs_url


def test_request_id_is_a_valid_xml_id() -> None:
    value = request_id("0-starts-with-a-digit")

    assert value.startswith("_")
    assert value == request_id("0-starts-with-a-digit")
    assert value != request_id("other")


@pytest.mark.parametrize("preset", list(SAMLPreset))
def test_presets_produce_valid_library_settings(preset: SAMLPreset) -> None:
    defaults = PRESETS[preset]
    config = SAMLConfig(
        name="corp",
        display_name="Corp",
        idp_entity_id=IDP_ENTITY_ID,
        idp_sso_url=IDP_SSO_URL,
        idp_certificates=(make_key_pair().cert_b64,),
        name_id_format=defaults.name_id_format,
        subject_attribute=defaults.subject_attribute,
    )
    settings = OneLogin_Saml2_Settings(
        config.library_settings(
            sp_entity_id="https://mail.example.org/api/auth/saml/corp/metadata",
            acs_url="https://mail.example.org/api/auth/saml/acs",
        )
    )

    security = settings.get_security_data()
    assert settings.is_strict()
    assert security["rejectDeprecatedAlgorithm"] is True
    assert security["requestedAuthnContext"] is False
