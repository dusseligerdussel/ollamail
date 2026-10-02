"""Group → role mapping rules (pure logic, #33)."""

from app.auth.policy import Rule, mapped_role
from app.users.models import UserRole

RULES = [
    Rule("CN=Mail-Admins,OU=Groups,DC=corp", None, UserRole.ADMIN),
    Rule("staff", "oidc:entra", UserRole.USER),
    Rule("ops", "ldap:corp", UserRole.ADMIN),
]


def test_highest_matching_role_wins_case_insensitively() -> None:
    groups = ["staff", "cn=mail-admins,ou=groups,dc=corp"]

    assert mapped_role(RULES, UserRole.USER, "oidc:entra", groups) is UserRole.ADMIN


def test_rules_of_other_providers_do_not_apply() -> None:
    assert mapped_role(RULES, UserRole.USER, "oidc:entra", ["ops"]) is UserRole.USER
    assert mapped_role(RULES, UserRole.USER, "ldap:corp", ["ops"]) is UserRole.ADMIN


def test_default_role_without_match() -> None:
    assert mapped_role(RULES, UserRole.ADMIN, "oidc:entra", []) is UserRole.ADMIN
    assert mapped_role(RULES, UserRole.USER, "oidc:entra", ["other"]) is UserRole.USER


def test_provider_role_counts_as_match_but_not_as_default() -> None:
    # LDAP admin_groups: "admin" counts like a matching rule ...
    assert mapped_role([], UserRole.USER, "ldap:corp", [], UserRole.ADMIN) is UserRole.ADMIN
    # ... "user" is only the provider's fallback and does not override the default role.
    assert mapped_role([], UserRole.ADMIN, "ldap:corp", [], UserRole.USER) is UserRole.ADMIN
