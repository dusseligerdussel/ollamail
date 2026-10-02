"""LDAP injection: user input must never change the structure of a filter."""

import pytest
from ldap3.operation.search import MATCH_EQUAL, parse_filter

from app.auth.providers.ldap.filters import (
    escape_filter_value,
    member_filter,
    nested_member_filter,
    normalize_dn,
    user_filter,
    validate_filter_template,
)

TEMPLATE = "(&(objectClass=user)(sAMAccountName={login}))"

INJECTIONS = [
    "*",
    "admin*",
    "*)(uid=*",
    "admin)(|(uid=*",
    "admin)(&)",
    "x)(objectClass=*))(|(uid=",
    "\\2a",
    "admin\x00",
    "a=b",
    "a>=b",
    "a~=b",
    "a:dn:=b",
    "{login}",
    "(",
    ")",
    "\\",
]


@pytest.mark.parametrize(
    ("value", "escaped"),
    [
        ("erika", "erika"),
        ("erika.mustermann@example.org", "erika.mustermann@example.org"),
        ("*", "\\2a"),
        ("(", "\\28"),
        (")", "\\29"),
        ("\\", "\\5c"),
        ("\x00", "\\00"),
        ("a=b", "a\\3db"),
        ("ä", "\\c3\\a4"),
        ("DOMAIN\\user", "DOMAIN\\5cuser"),
    ],
)
def test_escape_filter_value(value: str, escaped: str) -> None:
    assert escape_filter_value(value) == escaped


@pytest.mark.parametrize("login", INJECTIONS)
def test_injected_logins_stay_one_equality_assertion(login: str) -> None:
    search_filter = user_filter(TEMPLATE, login)
    # Only the template's own parentheses remain unescaped.
    assert search_filter.count("(") == TEMPLATE.count("(")
    assert search_filter.count(")") == TEMPLATE.count(")")
    assert "*" not in search_filter
    assert "\x00" not in search_filter

    tree = parse_filter(search_filter, None, True, True, None, False)
    assertion = tree.elements[0].elements[1]
    # No presence, substring or extensible match.
    assert assertion.tag == MATCH_EQUAL
    assert assertion.assertion["attr"] == "sAMAccountName"


def test_injected_login_value_round_trips() -> None:
    from ldap3.protocol.convert import prepare_filter_for_sending

    login = "o(brien)*\\ä"
    escaped = escape_filter_value(login)
    assert prepare_filter_for_sending(escaped) == login.encode()


def test_template_uses_plain_replacement_not_format() -> None:
    template = "(&(cn={login})(description={login}))"
    assert user_filter(template, "{0.__class__}") == (
        "(&(cn=\\7b0.__class__\\7d)(description=\\7b0.__class__\\7d))"
    )


def test_member_filters_escape_dns() -> None:
    dn = "uid=o(brien)*,ou=people,dc=example,dc=org"
    assert member_filter("(objectClass=groupOfNames)", "member", [dn]) == (
        "(&(objectClass=groupOfNames)"
        "(member=uid\\3do\\28brien\\29\\2a\\2cou\\3dpeople\\2cdc\\3dexample\\2cdc\\3dorg))"
    )
    assert member_filter("(objectClass=group)", "member", ["cn=a", "cn=b"]) == (
        "(&(objectClass=group)(|(member=cn\\3da)(member=cn\\3db)))"
    )
    assert nested_member_filter("(objectClass=group)", "member", "cn=x)(") == (
        "(&(objectClass=group)(member:1.2.840.113556.1.4.1941:=cn\\3dx\\29\\28))"
    )


@pytest.mark.parametrize(
    "template",
    [
        "(uid={login})",
        "(&(objectClass=person)(|(sAMAccountName={login})(userPrincipalName={login})))",
    ],
)
def test_valid_templates(template: str) -> None:
    assert validate_filter_template(f" {template} ", placeholder=True) == template


@pytest.mark.parametrize(
    "template",
    [
        "uid={login}",
        "(uid={login}",
        "(uid={login}))",
        "(uid={login})(cn=x)",
        "(uid=x)",
        "(uid={login})(",
        "(uid={0})",
        "(&(uid={login})(cn={other}))",
    ],
)
def test_invalid_templates(template: str) -> None:
    with pytest.raises(ValueError):
        validate_filter_template(template, placeholder=True)


def test_normalize_dn() -> None:
    assert normalize_dn("CN=Domain Admins , OU=Groups,DC=Example,DC=org") == (
        "cn=domain admins,ou=groups,dc=example,dc=org"
    )
    with pytest.raises(ValueError):
        normalize_dn("not a dn")
