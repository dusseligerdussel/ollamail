"""Building LDAP search filters and handling DNs without injection.

User input never becomes filter syntax: every value inserted into a filter goes through
``escape_filter_value``, which hex-escapes (RFC 4515) every byte outside a small set of
harmless ASCII characters. That covers the RFC's mandatory escapes (``*``, ``(``, ``)``,
``\\``, NUL) and also characters that some parsers treat specially (``=``, ``~``, ``<``,
``>``, ``&``, ``|``, ``!``, ``:``) as well as non-ASCII letters (sent as UTF-8 octets).
Filter templates come from the admin and contain the placeholder ``{login}``; they are
filled by plain string replacement, never by ``str.format``.
"""

import string

from ldap3.utils.dn import parse_dn

LOGIN_PLACEHOLDER = "{login}"
# Matching rule OID of AD's LDAP_MATCHING_RULE_IN_CHAIN (transitive group membership).
MATCHING_RULE_IN_CHAIN = "1.2.840.113556.1.4.1941"

_SAFE = frozenset(string.ascii_letters + string.digits + "._-@ ")


def escape_filter_value(value: str) -> str:
    """Escape a value for use inside an LDAP filter assertion (RFC 4515)."""
    out: list[str] = []
    for char in value:
        if char in _SAFE:
            out.append(char)
        else:
            out.extend(f"\\{byte:02x}" for byte in char.encode("utf-8"))
    return "".join(out)


def validate_filter_template(template: str, *, placeholder: bool) -> str:
    """Check that an admin-supplied filter is a single parenthesised filter.

    With ``placeholder`` it must contain ``{login}`` (and no other braces). Raises
    ``ValueError`` with a message for the admin.
    """
    template = template.strip()
    if not (template.startswith("(") and template.endswith(")")):
        raise ValueError("filter must be enclosed in parentheses")
    depth = 0
    for index, char in enumerate(template):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0 or (depth == 0 and index != len(template) - 1):
                raise ValueError("filter has unbalanced parentheses")
    if depth != 0:
        raise ValueError("filter has unbalanced parentheses")
    rest = template.replace(LOGIN_PLACEHOLDER, "")
    if placeholder and LOGIN_PLACEHOLDER not in template:
        raise ValueError("filter must contain the placeholder {login}")
    if "{" in rest or "}" in rest:
        raise ValueError("filter may only contain the placeholder {login}")
    return template


def user_filter(template: str, login: str) -> str:
    """The user search filter for ``login``."""
    return template.replace(LOGIN_PLACEHOLDER, escape_filter_value(login))


def member_filter(group_filter: str, member_attribute: str, member_dns: list[str]) -> str:
    """Groups (``group_filter``) that list any of ``member_dns`` in ``member_attribute``."""
    members = "".join(f"({member_attribute}={escape_filter_value(dn)})" for dn in member_dns)
    if len(member_dns) > 1:
        members = f"(|{members})"
    return f"(&{group_filter}{members})"


def nested_member_filter(group_filter: str, member_attribute: str, member_dn: str) -> str:
    """AD only: all groups ``member_dn`` belongs to, directly or through nested groups."""
    value = escape_filter_value(member_dn)
    return f"(&{group_filter}({member_attribute}:{MATCHING_RULE_IN_CHAIN}:={value}))"


def normalize_dn(dn: str) -> str:
    """Canonical form for comparing DNs: lower case, no spaces around separators.

    Raises ``ValueError`` for strings that are not a DN.
    """
    try:
        parts = parse_dn(dn.strip(), escape=False, strip=True)
    except Exception as exc:  # ldap3 raises its own exception types
        raise ValueError("invalid DN") from exc
    if not parts:
        raise ValueError("invalid DN")
    rdns: list[str] = []
    current: list[str] = []
    for attribute, value, separator in parts:
        current.append(f"{attribute.strip().lower()}={value.strip().lower()}")
        if separator != "+":
            rdns.append("+".join(current))
            current = []
    if current:
        rdns.append("+".join(current))
    return ",".join(rdns)
