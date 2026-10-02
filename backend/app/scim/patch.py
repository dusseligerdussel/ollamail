"""PATCH requests (RFC 7644 §3.5.2) in the variants Entra ID and Okta send.

Both send the same operations in different shapes; ``operations`` brings them into one:

* ``op`` in any case (Entra ID: ``Replace``, ``Add``, ``Remove``).
* With ``path`` (``active``, ``name.givenName``, ``emails[type eq "work"].value``,
  ``members[value eq "<id>"]``) or without, then ``value`` is an object whose keys are
  paths (Okta: ``{"active": false}``; Entra ID also ``{"name.givenName": "..."}``).
* Booleans as JSON booleans or as strings (``"False"``, older Entra ID behaviour).
* Member lists as ``[{"value": "<id>"}]``.
"""

import re
from dataclasses import dataclass
from typing import Any

from app.scim.errors import ScimError, invalid_value
from app.scim.filters import Condition, normalize_path, parse_filter

PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
_OPS = frozenset({"add", "replace", "remove"})
_MAX_OPERATIONS = 1000
_PATH = re.compile(
    r"^(?P<attr>[^\[\]]+?)(?:\[(?P<filter>[^\[\]]*)\])?(?:\.(?P<sub>[A-Za-z0-9_$-]+))?$"
)
# Value filters allowed inside a path: emails[type eq "work"], members[value eq "..."].
_PATH_FILTER_ATTRIBUTES = frozenset({"type", "primary", "value"})


@dataclass(frozen=True)
class Operation:
    op: str
    # Normalised attribute (``active``, ``name.givenname``, ``members``), ``None`` never.
    attribute: str
    value: Any = None
    # Value filter of the path (``members[value eq "x"]`` → ``value eq "x"``).
    selector: Condition | None = None
    # Sub-attribute after a value filter (``emails[type eq "work"].value`` → ``value``).
    sub_attribute: str | None = None


def invalid_path(detail: str = "The path is not supported.") -> ScimError:
    return ScimError(400, detail, "invalidPath")


def parse_path(path: str) -> tuple[str, Condition | None, str | None]:
    match = _PATH.match(path.strip())
    if match is None:
        raise invalid_path()
    attribute = normalize_path(match["attr"])
    selector = None
    if match["filter"] is not None:
        conditions = parse_filter(match["filter"], _PATH_FILTER_ATTRIBUTES)
        if len(conditions) != 1:
            raise invalid_path()
        selector = conditions[0]
    sub = match["sub"].lower() if match["sub"] else None
    if sub is not None and selector is None:
        # ``name.givenName`` is matched by the attribute pattern; only value paths get here.
        attribute = f"{attribute}.{sub}"
        sub = None
    return attribute, selector, sub


def operations(body: Any) -> list[Operation]:
    """The operations of a PATCH body; 400 for anything malformed."""
    if not isinstance(body, dict):
        raise invalid_value("The request body must be a JSON object.")
    schemas = body.get("schemas")
    if isinstance(schemas, list) and schemas and PATCH_SCHEMA not in schemas:
        raise invalid_value("The PatchOp schema is missing.")
    raw = body.get("Operations", body.get("operations"))
    if not isinstance(raw, list) or not raw or len(raw) > _MAX_OPERATIONS:
        raise invalid_value("Operations must be a non-empty list.")
    result: list[Operation] = []
    for item in raw:
        if not isinstance(item, dict):
            raise invalid_value("Each operation must be an object.")
        op = str(item.get("op", "")).strip().lower()
        if op not in _OPS:
            raise invalid_value("Unsupported operation.")
        path = item.get("path")
        value = item.get("value")
        if path is not None and not isinstance(path, str):
            raise invalid_path()
        if path:
            attribute, selector, sub = parse_path(path)
            result.append(Operation(op, attribute, value, selector, sub))
        elif op == "remove":
            raise ScimError(400, "Remove needs a path.", "noTarget")
        elif isinstance(value, dict):
            result.extend(_from_object(op, value))
        else:
            raise invalid_value("Operations without path need an object as value.")
    return result


def _from_object(op: str, value: dict[str, Any]) -> list[Operation]:
    result: list[Operation] = []
    for key, item in value.items():
        if not isinstance(key, str):
            continue
        attribute, selector, sub = parse_path(key)
        if attribute.startswith("urn:"):
            # Extension schemas (enterprise user: department, manager ...) are not stored.
            continue
        result.append(Operation(op, attribute, item, selector, sub))
    return result


def as_bool(value: Any) -> bool:
    """A boolean also from ``"True"``/``"False"`` strings; 400 for anything else."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    raise invalid_value("Expected a boolean.")


def as_text(value: Any, max_length: int, *, required: bool = True) -> str | None:
    """A trimmed string within ``max_length``; 400 if missing (``required``) or too long."""
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise invalid_value("A required attribute is missing.")
        return None
    if not isinstance(value, str):
        raise invalid_value("Expected a string.")
    text = str(value).strip()
    if len(text) > max_length:
        raise invalid_value("A value is too long.")
    return text
