"""The request sequence of Okta's SCIM 2.0 provisioning (Okta's SCIM test suite).

Okta pages with ``startIndex``/``count``, updates users with ``PUT``, deactivates with a
path-less ``PATCH``, renames groups with a path-less ``replace`` that also carries ``id``,
and removes members with ``members[value eq "..."]``.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import SCIM_PROVIDER, Identity
from app.users.models import User
from tests.scim.conftest import GROUP_SCHEMA, USER_SCHEMA, ok, patch_body

pytestmark = pytest.mark.db

OKTA_USER = {
    "schemas": [USER_SCHEMA],
    "userName": "test.user@okta.example",
    "name": {"givenName": "Test", "familyName": "User"},
    "emails": [{"primary": True, "value": "test.user@okta.example", "type": "work"}],
    "displayName": "Test User",
    "locale": "en-US",
    "externalId": "00ujl29u0le5T6Aj10h7",
    "groups": [],
    "password": "1mz050nq",
    "active": True,
}


async def test_user_lifecycle(idp: AsyncClient, db_session: AsyncSession) -> None:
    page = ok(
        await idp.get(
            "/Users",
            params={
                "filter": 'userName eq "test.user@okta.example"',
                "startIndex": 1,
                "count": 100,
            },
        )
    )
    assert page == {
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
        "totalResults": 0,
        "startIndex": 1,
        "itemsPerPage": 0,
        "Resources": [],
    }
    created = ok(await idp.post("/Users", json=OKTA_USER), 201)
    user_id = created["id"]
    assert created["displayName"] == "Test User"
    assert "password" not in created

    # Okta updates profiles with PUT (full resource).
    replaced = ok(
        await idp.put(
            f"/Users/{user_id}",
            json={
                **OKTA_USER,
                "id": user_id,
                "name": {"givenName": "Another", "familyName": "User"},
                "displayName": "Another User",
            },
        )
    )
    assert replaced["displayName"] == "Another User"

    # Deactivation: PATCH without path.
    deactivated = ok(
        await idp.patch(
            f"/Users/{user_id}", json=patch_body({"op": "replace", "value": {"active": False}})
        )
    )
    assert deactivated["active"] is False
    user = await db_session.get(User, uuid.UUID(user_id))
    assert user is not None
    await db_session.refresh(user)
    assert not user.is_active

    # Reactivation.
    reactivated = ok(
        await idp.patch(
            f"/Users/{user_id}", json=patch_body({"op": "replace", "value": {"active": True}})
        )
    )
    assert reactivated["active"] is True

    # Paging over all users.
    second = ok(
        await idp.post(
            "/Users",
            json={
                **OKTA_USER,
                "userName": "second@okta.example",
                "emails": [{"value": "second@okta.example", "primary": True}],
                "externalId": "00u2",
            },
        ),
        201,
    )
    first_page = ok(await idp.get("/Users", params={"startIndex": 1, "count": 1}))
    second_page = ok(await idp.get("/Users", params={"startIndex": 2, "count": 1}))
    assert first_page["totalResults"] == second_page["totalResults"] == 2
    assert first_page["itemsPerPage"] == 1
    ids = {first_page["Resources"][0]["id"], second_page["Resources"][0]["id"]}
    assert ids == {user_id, second["id"]}
    counted = ok(await idp.get("/Users", params={"count": 0}))
    assert counted["totalResults"] == 2 and counted["Resources"] == []


async def test_group_push(idp: AsyncClient, db_session: AsyncSession) -> None:
    user_id = ok(await idp.post("/Users", json=OKTA_USER), 201)["id"]
    group = ok(
        await idp.post(
            "/Groups", json={"schemas": [GROUP_SCHEMA], "displayName": "Test SCIMv2", "members": []}
        ),
        201,
    )
    group_id = group["id"]
    found = ok(
        await idp.get(
            "/Groups",
            params={"filter": 'displayName eq "Test SCIMv2"', "startIndex": 1, "count": 100},
        )
    )
    assert [g["id"] for g in found["Resources"]] == [group_id]

    ok(
        await idp.patch(
            f"/Groups/{group_id}",
            json=patch_body(
                {"op": "replace", "value": {"id": group_id, "displayName": "Test SCIMv2 renamed"}}
            ),
        ),
        204,
    )
    ok(
        await idp.patch(
            f"/Groups/{group_id}",
            json=patch_body(
                {
                    "op": "add",
                    "path": "members",
                    "value": [{"value": user_id, "display": "test.user@okta.example"}],
                }
            ),
        ),
        204,
    )
    fetched = ok(await idp.get(f"/Groups/{group_id}"))
    assert fetched["displayName"] == "Test SCIMv2 renamed"
    assert [m["value"] for m in fetched["members"]] == [user_id]

    identity = await db_session.scalar(
        select(Identity).where(
            Identity.user_id == uuid.UUID(user_id), Identity.provider == SCIM_PROVIDER
        )
    )
    assert identity is not None
    await db_session.refresh(identity)
    assert identity.groups == ["Test SCIMv2 renamed"]

    ok(
        await idp.patch(
            f"/Groups/{group_id}",
            json=patch_body({"op": "remove", "path": f'members[value eq "{user_id}"]'}),
        ),
        204,
    )
    await db_session.refresh(identity)
    assert identity.groups == []

    # Older Okta integrations replace the whole group with PUT.
    replaced = ok(
        await idp.put(
            f"/Groups/{group_id}",
            json={
                "schemas": [GROUP_SCHEMA],
                "id": group_id,
                "displayName": "Test SCIMv2",
                "members": [{"value": user_id, "display": "test.user@okta.example"}],
            },
        )
    )
    assert [m["value"] for m in replaced["members"]] == [user_id]
    await db_session.refresh(identity)
    assert identity.groups == ["Test SCIMv2"]

    ok(await idp.delete(f"/Groups/{group_id}"), 204)
    await db_session.refresh(identity)
    assert identity.groups == []
