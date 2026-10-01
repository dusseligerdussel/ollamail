"""Directory client: unit tests and integration tests against a real slapd."""

import uuid
from typing import Any

import pytest

from app.auth.providers.ldap.client import (
    LdapConfigError,
    LdapDirectoryClient,
    LdapUnavailableError,
    format_subject,
    valid_credentials_input,
)
from tests.auth.ldap.conftest import directory_settings
from tests.auth.ldap.slapd import (
    SERVICE_PASSWORD,
    SPECIAL_LOGIN,
    USER_PASSWORD,
    Slapd,
    group_dn,
    make_ca,
    pem,
)

STAFF = group_dn("staff")
ADMINS = group_dn("admins")


@pytest.mark.parametrize(
    ("login", "password", "valid"),
    [
        ("erika", "secret", True),
        ("erika", "", False),  # unauthenticated bind
        ("erika", "\x00", False),
        ("erika", "secret\x00", False),
        ("", "secret", False),
        ("eri\nka", "secret", False),
        ("x" * 257, "secret", False),
        ("jürgen", " ", True),
    ],
)
def test_credentials_input(login: str, password: str, valid: bool) -> None:
    assert valid_credentials_input(login, password) is valid


def test_object_guid_is_formatted_like_ad_tools() -> None:
    guid = uuid.UUID("2b1a5c3e-4d6f-4a8b-9c0d-1e2f3a4b5c6d")
    assert format_subject("objectGUID", guid.bytes_le) == str(guid)
    assert format_subject("entryUUID", b"6c6c-uuid") == "6c6c-uuid"
    assert format_subject("objectSid", b"\x01\x05\x00") == "010500"


def test_empty_password_never_contacts_the_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    client = LdapDirectoryClient(directory_settings(), SERVICE_PASSWORD)

    def fail(*args: Any) -> None:
        raise AssertionError("directory contacted")

    monkeypatch.setattr(client, "_service", fail)
    monkeypatch.setattr(client, "_open", fail)

    assert client.authenticate("erika", "") is None
    assert client.authenticate("erika", "\x00secret") is None


def test_active_directory_resolves_nested_groups_in_one_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = directory_settings(directory_type="active_directory", group_filter=None)
    client = LdapDirectoryClient(settings, SERVICE_PASSWORD)
    searches: list[str] = []

    def search(connection: object, base: str, search_filter: str, *args: Any, **kw: Any) -> Any:
        searches.append(search_filter)
        return [{"dn": "CN=Ollamail Admins,OU=Groups,DC=example,DC=org"}]

    monkeypatch.setattr(client, "_search", search)

    groups = client._groups(object(), "CN=Erika (HR),OU=Staff,DC=example,DC=org")

    assert searches == [
        "(&(objectClass=group)(member:1.2.840.113556.1.4.1941:="
        "CN\\3dErika \\28HR\\29\\2cOU\\3dStaff\\2cDC\\3dexample\\2cDC\\3dorg))"
    ]
    assert groups == {"cn=ollamail admins,ou=groups,dc=example,dc=org"}


# -- integration (real slapd) ------------------------------------------------------------


def _client(slapd: Slapd, **overrides: Any) -> LdapDirectoryClient:
    return LdapDirectoryClient(directory_settings(slapd, **overrides), SERVICE_PASSWORD)


@pytest.mark.ldap
@pytest.mark.parametrize("mode", ["ldaps", "starttls"])
def test_login_over_tls(slapd: Slapd, mode: str) -> None:
    url = slapd.ldaps_url if mode == "ldaps" else slapd.ldap_url
    client = _client(slapd, tls_mode=mode, server_urls=[url])

    user = client.authenticate("erika", USER_PASSWORD)

    assert user is not None
    assert user.dn == "uid=erika,ou=people,dc=example,dc=org"
    assert uuid.UUID(user.subject)  # entryUUID
    assert user.email == "Erika.Mustermann@Example.org"
    assert user.display_name == "Erika Mustermann"
    # admins contains staff, which contains erika.
    assert user.groups == {STAFF, ADMINS}
    assert not user.disabled


@pytest.mark.ldap
def test_wrong_password_unknown_user_and_disabled_account(slapd: Slapd) -> None:
    client = _client(slapd)

    assert client.authenticate("erika", "wrong password") is None
    assert client.authenticate("nobody", USER_PASSWORD) is None
    # userAccountControl 514 = NORMAL_ACCOUNT | ACCOUNTDISABLE; the password is right.
    assert client.authenticate("disabled", USER_PASSWORD) is None
    found = client.find_user("disabled")
    assert found is not None and found.disabled


@pytest.mark.ldap
def test_subject_is_stable(slapd: Slapd) -> None:
    client = _client(slapd)
    first = client.find_user("erika")
    second = client.authenticate("erika", USER_PASSWORD)
    assert first is not None and second is not None
    assert first.subject == second.subject


@pytest.mark.ldap
@pytest.mark.parametrize(
    "login",
    ["*", "eri*", "*a", "erika)(uid=*", "*)(|(uid=*", "erika)(|(objectClass=*", "\\2a", "e\\72ika"],
)
def test_injection_does_not_match_other_entries(slapd: Slapd, login: str) -> None:
    client = _client(slapd)

    assert client.find_user(login) is None
    assert client.authenticate(login, USER_PASSWORD) is None


@pytest.mark.ldap
def test_special_characters_in_login_and_dn(slapd: Slapd) -> None:
    client = _client(slapd)

    special = client.authenticate(SPECIAL_LOGIN, USER_PASSWORD)
    umlaut = client.authenticate("jürgen", USER_PASSWORD)

    assert special is not None
    # The DN uid=o(brien)*,... is escaped in the group search.
    assert special.groups == {STAFF, ADMINS}
    assert umlaut is not None
    assert umlaut.email == "juergen@example.org"


@pytest.mark.ldap
def test_cyclic_groups_terminate(slapd: Slapd) -> None:
    user = _client(slapd).find_user("jürgen")
    assert user is not None
    assert user.groups == {group_dn("cycle-a"), group_dn("cycle-b")}


@pytest.mark.ldap
def test_direct_groups_only(slapd: Slapd) -> None:
    user = _client(slapd, nested_groups=False).find_user("erika")
    assert user is not None
    assert user.groups == {STAFF}


@pytest.mark.ldap
def test_ambiguous_filter_matches_nobody(slapd: Slapd) -> None:
    # Two entries with uid=twin: refusing is safer than picking one.
    assert _client(slapd).authenticate("twin", USER_PASSWORD) is None


@pytest.mark.ldap
def test_failover_to_next_server(slapd: Slapd) -> None:
    client = _client(slapd, server_urls=["ldaps://127.0.0.1:1", slapd.ldaps_url])

    assert client.authenticate("erika", USER_PASSWORD) is not None
    checks = client.check_servers()
    assert [(c.ok, c.error) for c in checks] == [(False, "unreachable"), (True, None)]


@pytest.mark.ldap
def test_no_reachable_server(slapd: Slapd) -> None:
    client = _client(slapd, server_urls=["ldaps://127.0.0.1:1"], connect_timeout=1)
    with pytest.raises(LdapUnavailableError):
        client.authenticate("erika", USER_PASSWORD)


@pytest.mark.ldap
def test_untrusted_certificate_is_rejected(slapd: Slapd) -> None:
    _, other_ca = make_ca("other CA")
    client = _client(slapd, ca_certificate=pem(other_ca))

    with pytest.raises(LdapUnavailableError) as raised:
        client.authenticate("erika", USER_PASSWORD)
    assert raised.value.code == "tls_failed"
    # Without a CA certificate only the system CAs are trusted.
    assert not _client(slapd, ca_certificate=None).check_servers()[0].ok


@pytest.mark.ldap
def test_host_name_is_verified(slapd: Slapd) -> None:
    # The certificate is valid for 127.0.0.1 and ldap.test, not for localhost.
    client = _client(slapd, server_urls=[f"ldaps://localhost:{slapd.ldaps_port}"])
    [check] = client.check_servers()
    assert not check.ok
    assert check.error == "tls_failed"


@pytest.mark.ldap
def test_starttls_against_ldaps_port_fails(slapd: Slapd) -> None:
    client = _client(
        slapd, tls_mode="starttls", server_urls=[f"ldap://127.0.0.1:{slapd.ldaps_port}"]
    )
    assert not client.check_servers()[0].ok


@pytest.mark.ldap
def test_wrong_service_password(slapd: Slapd) -> None:
    client = LdapDirectoryClient(directory_settings(slapd), "wrong")

    with pytest.raises(LdapConfigError) as raised:
        client.authenticate("erika", USER_PASSWORD)
    assert raised.value.code == "service_bind_failed"
    assert client.check_servers()[0].error == "service_bind_failed"


@pytest.mark.ldap
def test_plaintext_only_when_configured(slapd: Slapd) -> None:
    client = _client(slapd, tls_mode="none", server_urls=[slapd.ldap_url])
    assert client.authenticate("erika", USER_PASSWORD) is not None


@pytest.mark.ldap
def test_missing_subject_attribute(slapd: Slapd) -> None:
    client = _client(slapd, subject_attribute="employeeNumber")
    with pytest.raises(LdapConfigError):
        client.find_user("erika")
