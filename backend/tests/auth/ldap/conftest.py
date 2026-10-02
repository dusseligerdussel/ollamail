"""Fixtures for LDAP tests: a real slapd (marker ``ldap``) and directory settings."""

import base64
import os
from collections.abc import Iterator
from typing import Any

import pytest

from app.auth.providers.ldap.settings import LdapDirectorySettings
from app.core.crypto import KeyRing, decode_key, set_keyring
from tests.auth.ldap.slapd import GROUPS, PEOPLE, SERVICE_DN, Slapd, find_slapd, start_slapd

REQUIRE_LDAP = os.environ.get("OLLAMAIL_TEST_REQUIRE_LDAP", "").lower() in {"1", "true", "yes"}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if find_slapd() is not None:
        return
    message = "slapd not installed (apt-get install slapd ldap-utils)"
    ldap_items = [item for item in items if item.get_closest_marker("ldap")]
    if ldap_items and REQUIRE_LDAP:
        raise pytest.UsageError(message)
    for item in ldap_items:
        item.add_marker(pytest.mark.skip(reason=message))


@pytest.fixture(scope="session")
def slapd(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Slapd]:
    binaries = find_slapd()
    assert binaries is not None
    server = start_slapd(tmp_path_factory.mktemp("slapd"), binaries)
    yield server
    server.stop()


def directory_settings(slapd: Slapd | None = None, **overrides: Any) -> LdapDirectorySettings:
    data: dict[str, Any] = {
        "directory_type": "openldap",
        "server_urls": [slapd.ldaps_url if slapd else "ldaps://ldap.example.org"],
        "tls_mode": "ldaps",
        "ca_certificate": slapd.ca_pem if slapd else None,
        "bind_dn": SERVICE_DN,
        "user_base_dn": PEOPLE,
        "group_base_dn": GROUPS,
    }
    data.update(overrides)
    return LdapDirectorySettings.model_validate(data)


@pytest.fixture
def keyring() -> Iterator[None]:
    """A process-wide key ring for ``EncryptedStr`` columns."""
    set_keyring(KeyRing(decode_key(base64.b64encode(os.urandom(32)).decode())))
    yield
    set_keyring(None)
