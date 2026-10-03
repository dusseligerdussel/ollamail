"""SCIM 2.0 endpoints under ``/scim/v2`` (public: ``<public url>/api/scim/v2``).

Authenticated with a bearer token from the admin area (``app.scim.tokens``), not with a
session, so the CSRF middleware exempts the prefix. Not part of the OpenAPI document: the
clients are IdPs, not the frontend. Request and response bodies are
``application/scim+json`` (plain ``application/json`` is accepted as well).
"""

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.db import get_db
from app.privacy.router import UserDeletionRequesterDep
from app.scim import resources, service
from app.scim.errors import ScimError, ScimRoute, not_found, scim_response
from app.scim.filters import parse_filter
from app.scim.patch import operations
from app.scim.tokens import ScimClientDep

CSRF_EXEMPT_PREFIX = resources.BASE_PATH + "/"

router = APIRouter(prefix=resources.BASE_PATH, route_class=ScimRoute, include_in_schema=False)

DbDep = Annotated[AsyncSession, Depends(get_db)]
FilterQuery = Annotated[str | None, Query(alias="filter")]
StartIndexQuery = Annotated[str | None, Query(alias="startIndex")]
CountQuery = Annotated[str | None, Query(alias="count")]
AttributesQuery = Annotated[str | None, Query(alias="attributes")]
ExcludedQuery = Annotated[str | None, Query(alias="excludedAttributes")]


async def _body(request: Request) -> Any:
    try:
        return json.loads(await request.body())
    except ValueError:
        raise ScimError(400, "The request body is not valid JSON.", "invalidSyntax") from None


def _int(value: str | None, default: int) -> int:
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError:
        raise ScimError(400, "startIndex and count must be integers.", "invalidValue") from None


def _paging(request: Request, start_index: str | None, count: str | None) -> tuple[int, int]:
    """1-based start index and page size (RFC 7644 §3.4.2.4), capped at ``max_results``."""
    settings: Settings = request.app.state.settings
    limit = settings.scim.max_results
    return max(1, _int(start_index, 1)), min(max(0, _int(count, limit)), limit)


def _created(resource: dict[str, Any]) -> Response:
    return scim_response(
        resource, status.HTTP_201_CREATED, {"Location": resource["meta"]["location"]}
    )


# -- discovery ---------------------------------------------------------------------------


@router.get("/ServiceProviderConfig")
async def service_provider_config(request: Request, _: ScimClientDep) -> Response:
    settings: Settings = request.app.state.settings
    return scim_response(resources.service_provider_config(request, settings.scim.max_results))


@router.get("/ResourceTypes")
async def resource_types(request: Request, _: ScimClientDep) -> Response:
    items = resources.resource_types(request)
    return scim_response(resources.list_response(items, len(items), 1))


@router.get("/ResourceTypes/{name}")
async def resource_type(name: str, request: Request, _: ScimClientDep) -> Response:
    for item in resources.resource_types(request):
        if item["id"].lower() == name.lower():
            return scim_response(item)
    raise not_found("Resource type")


@router.get("/Schemas")
async def schemas(request: Request, _: ScimClientDep) -> Response:
    items = resources.schemas(request)
    return scim_response(resources.list_response(items, len(items), 1))


@router.get("/Schemas/{schema_id}")
async def schema(schema_id: str, request: Request, _: ScimClientDep) -> Response:
    for item in resources.schemas(request):
        if item["id"].lower() == schema_id.lower():
            return scim_response(item)
    raise not_found("Schema")


# -- users -------------------------------------------------------------------------------


async def _user_resource(
    request: Request,
    db: AsyncSession,
    user_and_scim: tuple[Any, Any],
    attributes: str | None = None,
    excluded: str | None = None,
) -> dict[str, Any]:
    user, scim_user = user_and_scim
    groups = (await service.groups_of(db, [user.id])).get(user.id, [])
    return resources.project(
        resources.user_resource(request, user, scim_user, groups), attributes, excluded
    )


@router.get("/Users")
async def list_users(
    request: Request,
    _: ScimClientDep,
    db: DbDep,
    filter_: FilterQuery = None,
    start_index: StartIndexQuery = None,
    count: CountQuery = None,
    attributes: AttributesQuery = None,
    excluded: ExcludedQuery = None,
) -> Response:
    conditions = parse_filter(filter_, service.USER_FILTERS)
    start, size = _paging(request, start_index, count)
    total, rows = await service.list_users(db, conditions, start, size)
    groups = await service.groups_of(db, [user.id for user, _ in rows])
    items = [
        resources.project(
            resources.user_resource(request, user, scim_user, groups.get(user.id, [])),
            attributes,
            excluded,
        )
        for user, scim_user in rows
    ]
    return scim_response(resources.list_response(items, total, start))


@router.post("/Users")
async def create_user(request: Request, _: ScimClientDep, db: DbDep) -> Response:
    data = service.user_data(await _body(request))
    created = await service.create_user(db, request, data)
    return _created(await _user_resource(request, db, created))


@router.get("/Users/{user_id}")
async def get_user(
    user_id: str,
    request: Request,
    _: ScimClientDep,
    db: DbDep,
    attributes: AttributesQuery = None,
    excluded: ExcludedQuery = None,
) -> Response:
    found = await service.get_user(db, user_id)
    return scim_response(await _user_resource(request, db, found, attributes, excluded))


@router.put("/Users/{user_id}")
async def replace_user(user_id: str, request: Request, _: ScimClientDep, db: DbDep) -> Response:
    user, scim_user = await service.get_user(db, user_id)
    data = service.user_data(await _body(request))
    updated = await service.update_user(db, request, user, scim_user, data)
    return scim_response(await _user_resource(request, db, updated))


@router.patch("/Users/{user_id}")
async def patch_user(user_id: str, request: Request, _: ScimClientDep, db: DbDep) -> Response:
    user, scim_user = await service.get_user(db, user_id)
    ops = operations(await _body(request))
    data = service.patch_user(service.current_user_data(user, scim_user), ops)
    updated = await service.update_user(db, request, user, scim_user, data)
    return scim_response(await _user_resource(request, db, updated))


@router.delete("/Users/{user_id}")
async def delete_user(
    user_id: str,
    request: Request,
    _: ScimClientDep,
    db: DbDep,
    finisher: UserDeletionRequesterDep,
) -> Response:
    await service.delete_user(db, request, user_id, finisher)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# -- groups ------------------------------------------------------------------------------


def _wants_members(attributes: str | None, excluded: str | None) -> bool:
    def names(value: str | None) -> set[str]:
        return {name.strip().lower() for name in (value or "").split(",") if name.strip()}

    if attributes:
        return "members" in names(attributes)
    return "members" not in names(excluded)


async def _group_resources(
    request: Request,
    db: AsyncSession,
    groups: list[Any],
    attributes: str | None = None,
    excluded: str | None = None,
) -> list[dict[str, Any]]:
    with_members = _wants_members(attributes, excluded)
    members = await service.members_of(db, [g.id for g in groups]) if with_members else {}
    return [
        resources.project(
            resources.group_resource(
                request, group, members.get(group.id, []) if with_members else None
            ),
            attributes,
            excluded,
        )
        for group in groups
    ]


@router.get("/Groups")
async def list_groups(
    request: Request,
    _: ScimClientDep,
    db: DbDep,
    filter_: FilterQuery = None,
    start_index: StartIndexQuery = None,
    count: CountQuery = None,
    attributes: AttributesQuery = None,
    excluded: ExcludedQuery = None,
) -> Response:
    conditions = parse_filter(filter_, service.GROUP_FILTERS)
    start, size = _paging(request, start_index, count)
    total, groups = await service.list_groups(db, conditions, start, size)
    items = await _group_resources(request, db, groups, attributes, excluded)
    return scim_response(resources.list_response(items, total, start))


@router.post("/Groups")
async def create_group(request: Request, _: ScimClientDep, db: DbDep) -> Response:
    group = await service.create_group(db, service.group_data(await _body(request)))
    (resource,) = await _group_resources(request, db, [group])
    return _created(resource)


@router.get("/Groups/{group_id}")
async def get_group(
    group_id: str,
    request: Request,
    _: ScimClientDep,
    db: DbDep,
    attributes: AttributesQuery = None,
    excluded: ExcludedQuery = None,
) -> Response:
    group = await service.get_group(db, group_id)
    (resource,) = await _group_resources(request, db, [group], attributes, excluded)
    return scim_response(resource)


@router.put("/Groups/{group_id}")
async def replace_group(group_id: str, request: Request, _: ScimClientDep, db: DbDep) -> Response:
    group = await service.get_group(db, group_id)
    group = await service.update_group(db, group, service.group_data(await _body(request)))
    (resource,) = await _group_resources(request, db, [group])
    return scim_response(resource)


@router.patch("/Groups/{group_id}")
async def patch_group(group_id: str, request: Request, _: ScimClientDep, db: DbDep) -> Response:
    """Answered with 204 (RFC 7644 §3.5.2): groups can be large, and neither Entra ID nor
    Okta reads the body."""
    group = await service.get_group(db, group_id)
    ops = operations(await _body(request))
    current = service.GroupData(
        display_name=group.display_name,
        external_id=group.external_id,
        members={str(member) for member in await service.member_ids(db, group.id)},
    )
    await service.update_group(db, group, service.patch_group(current, ops))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/Groups/{group_id}")
async def delete_group(group_id: str, _: ScimClientDep, db: DbDep) -> Response:
    group = await service.get_group(db, group_id)
    await service.delete_group(db, group)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
