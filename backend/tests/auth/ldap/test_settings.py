from typing import Any

import pytest
from pydantic import ValidationError

from app.auth.providers.ldap.settings import DirectoryType, LdapDirectorySettings, TlsMode
from tests.auth.ldap.slapd import make_ca, pem

BASE: dict[str, Any] = {
    "server_urls": ["ldaps://dc1.example.org", "ldaps://dc2.example.org:3269"],
    "bind_dn": "CN=svc-ollamail,OU=Service,DC=example,DC=org",
    "user_base_dn": "DC=example,DC=org",
}


def test_active_directory_preset() -> None:
    settings = LdapDirectorySettings.model_validate(BASE)

    assert settings.directory_type is DirectoryType.ACTIVE_DIRECTORY
    assert settings.tls_mode is TlsMode.LDAPS
    assert settings.subject_attribute == "objectGUID"
    assert "sAMAccountName={login}" in settings.user_filter
    assert settings.group_filter == "(objectClass=group)"
    assert settings.nested_groups


def test_openldap_preset_and_overrides() -> None:
    settings = LdapDirectorySettings.model_validate(
        {**BASE, "directory_type": "openldap", "email_attribute": "mailPrimaryAddress"}
    )

    assert settings.subject_attribute == "entryUUID"
    assert settings.user_filter == "(&(objectClass=inetOrgPerson)(uid={login}))"
    assert settings.email_attribute == "mailPrimaryAddress"


def test_group_dns_are_normalised() -> None:
    settings = LdapDirectorySettings.model_validate(
        {**BASE, "admin_groups": ["CN=Ollamail Admins, OU=Groups,DC=example,DC=org"]}
    )
    assert settings.admin_groups == ["cn=ollamail admins,ou=groups,dc=example,dc=org"]


def test_ca_certificate_is_validated() -> None:
    _, ca = make_ca()
    settings = LdapDirectorySettings.model_validate({**BASE, "ca_certificate": pem(ca)})
    assert settings.ca_certificate is not None
    assert settings.ca_certificate.startswith("-----BEGIN CERTIFICATE-----")

    with pytest.raises(ValidationError):
        LdapDirectorySettings.model_validate({**BASE, "ca_certificate": "not a certificate"})


@pytest.mark.parametrize(
    "changes",
    [
        {"server_urls": []},
        {"server_urls": ["ldap://dc1.example.org"]},  # ldaps mode needs ldaps://
        {"server_urls": ["ldaps://dc1.example.org"], "tls_mode": "starttls"},
        {"server_urls": ["https://dc1.example.org"]},
        {"server_urls": ["ldaps://user:pw@dc1.example.org"]},
        {"server_urls": ["ldaps://dc1.example.org/dc=example"]},
        {"server_urls": ["ldaps://dc1.example.org:99999"]},
        {"bind_dn": "svc-ollamail"},
        {"user_filter": "(sAMAccountName=*)"},
        {"user_filter": "sAMAccountName={login}"},
        {"subject_attribute": "objectGUID)(x"},
        {"admin_groups": ["Domain Admins"]},
        {"connect_timeout": 0},
        {"directory_type": "novell"},
    ],
)
def test_invalid_settings(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        LdapDirectorySettings.model_validate({**BASE, **changes})


def test_starttls_and_plaintext_use_ldap_urls() -> None:
    for mode in ("starttls", "none"):
        settings = LdapDirectorySettings.model_validate(
            {**BASE, "server_urls": ["LDAP://DC1.example.org:389/"], "tls_mode": mode}
        )
        assert settings.server_urls == ["ldap://dc1.example.org:389"]
