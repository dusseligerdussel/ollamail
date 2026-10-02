"""SCIM resource representations (RFC 7643) and the discovery documents.

Only what ollamail uses is stored and returned: ``userName``, ``externalId``, the display
name, one e-mail address, ``active`` and group memberships. Further attributes the IdP
sends (phone numbers, addresses, enterprise extension ...) are accepted and dropped
(data minimisation, docs/PRIVACY.md); the ``Schemas`` document lists the stored ones.
"""

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from fastapi import Request

from app.auth.redirect_flow import API_PREFIX, public_origin
from app.core.config import Settings
from app.scim.models import ScimGroup, ScimUser
from app.users.models import User

BASE_PATH = "/scim/v2"
USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
SPC_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"
RESOURCE_TYPE_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:ResourceType"
SCHEMA_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Schema"


def base_url(request: Request) -> str:
    """Absolute URL of the SCIM endpoint as the IdP reaches it (behind ``/api``)."""
    settings: Settings = request.app.state.settings
    return public_origin(settings, request) + API_PREFIX + BASE_PATH


def _timestamp(value: datetime) -> str:
    return value.isoformat()


def _meta(
    request: Request, kind: str, id_: str, created: datetime, modified: datetime
) -> dict[str, Any]:
    return {
        "resourceType": kind,
        "created": _timestamp(created),
        "lastModified": _timestamp(modified),
        "location": f"{base_url(request)}/{kind}s/{id_}",
    }


def user_resource(
    request: Request,
    user: User,
    scim_user: ScimUser,
    groups: Sequence[ScimGroup] = (),
) -> dict[str, Any]:
    user_id = str(user.id)
    resource: dict[str, Any] = {
        "schemas": [USER_SCHEMA],
        "id": user_id,
        "userName": scim_user.user_name,
        "displayName": user.display_name,
        "name": {"formatted": user.display_name},
        "emails": [{"value": user.email, "type": "work", "primary": True}],
        "active": user.is_active,
        "groups": [
            {
                "value": str(group.id),
                "display": group.display_name,
                "$ref": f"{base_url(request)}/Groups/{group.id}",
                "type": "direct",
            }
            for group in groups
        ],
        "meta": _meta(
            request,
            "User",
            user_id,
            scim_user.created_at,
            max(scim_user.updated_at, user.updated_at),
        ),
    }
    if scim_user.external_id is not None:
        resource["externalId"] = scim_user.external_id
    return resource


def group_resource(
    request: Request, group: ScimGroup, members: Iterable[tuple[User, ScimUser]] | None
) -> dict[str, Any]:
    """``members`` ``None`` leaves them out (``excludedAttributes=members``)."""
    group_id = str(group.id)
    resource: dict[str, Any] = {
        "schemas": [GROUP_SCHEMA],
        "id": group_id,
        "displayName": group.display_name,
        "meta": _meta(request, "Group", group_id, group.created_at, group.updated_at),
    }
    if group.external_id is not None:
        resource["externalId"] = group.external_id
    if members is not None:
        resource["members"] = [
            {
                "value": str(user.id),
                "display": user.display_name,
                "$ref": f"{base_url(request)}/Users/{user.id}",
                "type": "User",
            }
            for user, _ in members
        ]
    return resource


# Always returned (RFC 7643 §7 "returned": "always").
_ALWAYS = frozenset({"id", "schemas"})


def project(
    resource: dict[str, Any], attributes: str | None, excluded: str | None
) -> dict[str, Any]:
    """Apply ``attributes``/``excludedAttributes`` (top-level attributes, case-insensitive)."""

    def names(value: str | None) -> set[str]:
        return {
            name.strip().rsplit(":", 1)[-1].split(".", 1)[0].lower()
            for name in (value or "").split(",")
            if name.strip()
        }

    wanted, unwanted = names(attributes), names(excluded)
    if not wanted and not unwanted:
        return resource
    result: dict[str, Any] = {}
    for key, value in resource.items():
        lowered = key.lower()
        always = lowered in _ALWAYS or lowered == "meta"
        if always or (lowered in wanted if wanted else lowered not in unwanted):
            result[key] = value
    return result


def list_response(resources: list[dict[str, Any]], total: int, start_index: int) -> dict[str, Any]:
    return {
        "schemas": [LIST_SCHEMA],
        "totalResults": total,
        "startIndex": start_index,
        "itemsPerPage": len(resources),
        "Resources": resources,
    }


def service_provider_config(request: Request, max_results: int) -> dict[str, Any]:
    return {
        "schemas": [SPC_SCHEMA],
        "documentationUri": "https://github.com/dusseligerdussel/ollamail/blob/main/docs/auth/scim.md",
        "patch": {"supported": True},
        "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
        "filter": {"supported": True, "maxResults": max_results},
        "changePassword": {"supported": False},
        "sort": {"supported": False},
        "etag": {"supported": False},
        "authenticationSchemes": [
            {
                "type": "oauthbearertoken",
                "name": "Bearer token",
                "description": "Token created in the ollamail admin area (SCIM provisioning).",
                "primary": True,
            }
        ],
        "meta": {
            "resourceType": "ServiceProviderConfig",
            "location": f"{base_url(request)}/ServiceProviderConfig",
        },
    }


def _attribute(
    name: str,
    *,
    type_: str = "string",
    required: bool = False,
    multi: bool = False,
    mutability: str = "readWrite",
    uniqueness: str = "none",
    case_exact: bool = False,
    returned: str = "default",
    sub: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    attribute: dict[str, Any] = {
        "name": name,
        "type": type_,
        "multiValued": multi,
        "required": required,
        "caseExact": case_exact,
        "mutability": mutability,
        "returned": returned,
        "uniqueness": uniqueness,
    }
    if sub is not None:
        attribute["subAttributes"] = sub
    return attribute


def _reference_list(name: str, mutability: str) -> dict[str, Any]:
    return _attribute(
        name,
        type_="complex",
        multi=True,
        mutability=mutability,
        sub=[
            _attribute("value", mutability="immutable"),
            _attribute("display", mutability="readOnly"),
            _attribute("$ref", type_="reference", mutability="immutable"),
            _attribute("type", mutability="immutable"),
        ],
    )


USER_ATTRIBUTES = [
    _attribute("userName", required=True, uniqueness="server"),
    _attribute("externalId", case_exact=True),
    _attribute("displayName"),
    _attribute("name", type_="complex", sub=[_attribute("formatted")]),
    _attribute(
        "emails",
        type_="complex",
        multi=True,
        sub=[
            _attribute("value"),
            _attribute("type"),
            _attribute("primary", type_="boolean"),
        ],
    ),
    _attribute("active", type_="boolean"),
    _reference_list("groups", "readOnly"),
]
GROUP_ATTRIBUTES = [
    _attribute("displayName", required=True),
    _attribute("externalId", case_exact=True),
    _reference_list("members", "readWrite"),
]


def schemas(request: Request) -> list[dict[str, Any]]:
    return [
        {
            "schemas": [SCHEMA_SCHEMA],
            "id": USER_SCHEMA,
            "name": "User",
            "description": "User account",
            "attributes": USER_ATTRIBUTES,
            "meta": {
                "resourceType": "Schema",
                "location": f"{base_url(request)}/Schemas/{USER_SCHEMA}",
            },
        },
        {
            "schemas": [SCHEMA_SCHEMA],
            "id": GROUP_SCHEMA,
            "name": "Group",
            "description": "Group",
            "attributes": GROUP_ATTRIBUTES,
            "meta": {
                "resourceType": "Schema",
                "location": f"{base_url(request)}/Schemas/{GROUP_SCHEMA}",
            },
        },
    ]


def resource_types(request: Request) -> list[dict[str, Any]]:
    return [
        {
            "schemas": [RESOURCE_TYPE_SCHEMA],
            "id": kind,
            "name": kind,
            "endpoint": f"/{kind}s",
            "description": description,
            "schema": schema,
            "schemaExtensions": [],
            "meta": {
                "resourceType": "ResourceType",
                "location": f"{base_url(request)}/ResourceTypes/{kind}",
            },
        }
        for kind, schema, description in (
            ("User", USER_SCHEMA, "User account"),
            ("Group", GROUP_SCHEMA, "Group"),
        )
    ]
