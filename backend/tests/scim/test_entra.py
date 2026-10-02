"""The request sequence of the Entra ID provisioning service (and its SCIM validator).

Bodies follow Microsoft's documented requests ("Develop a SCIM endpoint", request and
response examples), including the PascalCase ``op`` values, value-path filters on
``emails``, ``"False"`` as string and ``excludedAttributes=members`` on groups.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.models import audit_events
from app.auth.models import SCIM_PROVIDER, Identity
from app.scim.models import ScimGroup, ScimUser
from app.users.models import User
from tests.scim.conftest import ENTERPRISE, GROUP_SCHEMA, USER_SCHEMA, ok, patch_body

pytestmark = pytest.mark.db

ENTRA_USER = {
    "schemas": [USER_SCHEMA, ENTERPRISE],
    "externalId": "0a21f0f2-8d2a-4f8e-bf98-7363c4aed4ef",
    "userName": "Test_User_ab6490ee@contoso.example",
    "active": True,
    "emails": [{"primary": True, "type": "work", "value": "test_user_ab6490ee@contoso.example"}],
    "meta": {"resourceType": "User"},
    "name": {"formatted": "Given Family", "familyName": "Family", "givenName": "Given"},
    "roles": [],
    ENTERPRISE: {"department": "Sales", "employeeNumber": "4711"},
}


async def _entra_create_user(idp: AsyncClient, body: dict[str, object] = ENTRA_USER) -> str:
    # Entra ID first looks the user up by its matching attribute (userName) ...
    found = ok(await idp.get("/Users", params={"filter": f'userName eq "{body["userName"]}"'}))
    assert found["totalResults"] == 0
    assert found["Resources"] == []
    # ... and creates it if there is none.
    response = await idp.post("/Users", json=body)
    created = ok(response, 201)
    assert response.headers["location"] == created["meta"]["location"]
    return str(created["id"])


async def test_create_get_update_disable_delete_user(
    idp: AsyncClient, db_session: AsyncSession
) -> None:
    user_id = await _entra_create_user(idp)

    user = await db_session.get(User, uuid.UUID(user_id))
    assert user is not None
    assert user.email == "test_user_ab6490ee@contoso.example"
    assert user.display_name == "Given Family"
    assert user.is_active
    identity = await db_session.scalar(
        select(Identity).where(Identity.user_id == user.id, Identity.provider == SCIM_PROVIDER)
    )
    assert identity is not None and identity.groups == []

    # Lookup by filter (case-insensitive userName) and by ID.
    listed = ok(
        await idp.get(
            "/Users", params={"filter": 'userName eq "test_user_AB6490EE@contoso.example"'}
        )
    )
    assert listed["totalResults"] == 1
    resource = listed["Resources"][0]
    assert resource["id"] == user_id
    assert resource["externalId"] == ENTRA_USER["externalId"]
    assert resource["active"] is True
    assert resource["emails"] == [
        {"value": "test_user_ab6490ee@contoso.example", "type": "work", "primary": True}
    ]
    # Extension attributes are not stored (data minimisation).
    assert ENTERPRISE not in resource
    fetched = ok(await idp.get(f"/Users/{user_id}"))
    assert fetched["userName"] == ENTRA_USER["userName"]
    by_external = ok(
        await idp.get("/Users", params={"filter": f'externalId eq "{ENTRA_USER["externalId"]}"'})
    )
    assert by_external["totalResults"] == 1

    # Update multi-valued and single-valued attributes.
    updated = ok(
        await idp.patch(
            f"/Users/{user_id}",
            json=patch_body(
                {
                    "op": "Replace",
                    "path": 'emails[type eq "work"].value',
                    "value": "updated_ab6490ee@contoso.example",
                },
                {"op": "Replace", "path": "name.familyName", "value": "updatedFamilyName"},
                {"op": "Add", "path": "displayName", "value": "Given Updated"},
            ),
        )
    )
    assert updated["emails"][0]["value"] == "updated_ab6490ee@contoso.example"
    assert updated["displayName"] == "Given Updated"
    renamed = ok(
        await idp.patch(
            f"/Users/{user_id}",
            json=patch_body(
                {"op": "Replace", "path": "userName", "value": "5b50642d@contoso.example"}
            ),
        )
    )
    assert renamed["userName"] == "5b50642d@contoso.example"

    # Disable (older Entra ID behaviour: the boolean as string).
    disabled = ok(
        await idp.patch(
            f"/Users/{user_id}",
            json=patch_body({"op": "Replace", "path": "active", "value": "False"}),
        )
    )
    assert disabled["active"] is False
    await db_session.refresh(user)
    assert not user.is_active

    # Delete: user, identity and SCIM row are gone.
    ok(await idp.delete(f"/Users/{user_id}"), 204)
    db_session.expunge_all()
    assert await db_session.get(User, uuid.UUID(user_id)) is None
    assert await db_session.scalar(select(func.count()).select_from(ScimUser)) == 0
    assert (await idp.get(f"/Users/{user_id}")).status_code == 404

    actions = list(
        await db_session.scalars(
            select(audit_events.c.action)
            .where(audit_events.c.target_id == user_id)
            .order_by(audit_events.c.occurred_at, audit_events.c.id)
        )
    )
    assert actions == [
        "user.created",
        "user.updated",
        "user.updated",
        "user.deactivated",
        "user.deleted",
    ]


async def test_duplicate_user_name_conflicts(idp: AsyncClient) -> None:
    await _entra_create_user(idp)
    response = await idp.post(
        "/Users",
        json={**ENTRA_USER, "emails": [{"value": "other@contoso.example", "primary": True}]},
    )
    error = ok(response, 409)
    assert error["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]
    assert error["status"] == "409"
    assert error["scimType"] == "uniqueness"


async def test_groups_with_members(idp: AsyncClient, db_session: AsyncSession) -> None:
    user_id = await _entra_create_user(idp)

    found = ok(
        await idp.get(
            "/Groups",
            params={"excludedAttributes": "members", "filter": 'displayName eq "Sales"'},
        )
    )
    assert found["totalResults"] == 0
    created = ok(
        await idp.post(
            "/Groups",
            json={
                "schemas": [GROUP_SCHEMA],
                "externalId": "8aa1a0c0-c4c3-4bc0-b4a5-2ef676900159",
                "displayName": "Sales",
                "meta": {"resourceType": "Group"},
            },
        ),
        201,
    )
    group_id = created["id"]
    assert created["members"] == []
    fetched = ok(await idp.get(f"/Groups/{group_id}", params={"excludedAttributes": "members"}))
    assert "members" not in fetched
    assert fetched["externalId"] == "8aa1a0c0-c4c3-4bc0-b4a5-2ef676900159"

    ok(
        await idp.patch(
            f"/Groups/{group_id}",
            json=patch_body(
                {"op": "Add", "path": "members", "value": [{"$ref": None, "value": user_id}]}
            ),
        ),
        204,
    )
    # Entra ID checks a membership with a value-path filter.
    member = ok(
        await idp.get(
            "/Groups",
            params={
                "excludedAttributes": "members",
                "filter": f'id eq "{group_id}" and members[value eq "{user_id}"]',
            },
        )
    )
    assert member["totalResults"] == 1
    user = ok(await idp.get(f"/Users/{user_id}"))
    assert [g["value"] for g in user["groups"]] == [group_id]

    identity = await db_session.scalar(
        select(Identity).where(
            Identity.user_id == uuid.UUID(user_id), Identity.provider == SCIM_PROVIDER
        )
    )
    assert identity is not None
    await db_session.refresh(identity)
    assert identity.groups == ["8aa1a0c0-c4c3-4bc0-b4a5-2ef676900159", "Sales"]

    ok(
        await idp.patch(
            f"/Groups/{group_id}",
            json=patch_body({"op": "Replace", "path": "displayName", "value": "Sales EMEA"}),
        ),
        204,
    )
    await db_session.refresh(identity)
    assert "Sales EMEA" in identity.groups

    # Both removal forms: value list (older) and value-path filter (current).
    ok(
        await idp.patch(
            f"/Groups/{group_id}",
            json=patch_body({"op": "Remove", "path": "members", "value": [{"value": user_id}]}),
        ),
        204,
    )
    await db_session.refresh(identity)
    assert identity.groups == []
    ok(
        await idp.patch(
            f"/Groups/{group_id}",
            json=patch_body({"op": "Add", "path": "members", "value": [{"value": user_id}]}),
        ),
        204,
    )
    ok(
        await idp.patch(
            f"/Groups/{group_id}",
            json=patch_body({"op": "Remove", "path": f'members[value eq "{user_id}"]'}),
        ),
        204,
    )
    after = ok(await idp.get(f"/Groups/{group_id}"))
    assert after["members"] == []

    ok(await idp.delete(f"/Groups/{group_id}"), 204)
    assert await db_session.scalar(select(func.count()).select_from(ScimGroup)) == 0
    assert (await idp.get(f"/Groups/{group_id}")).status_code == 404


async def test_unknown_member_is_rejected(idp: AsyncClient) -> None:
    group = ok(await idp.post("/Groups", json={"displayName": "Sales"}), 201)
    error = ok(
        await idp.patch(
            f"/Groups/{group['id']}",
            json=patch_body(
                {"op": "Add", "path": "members", "value": [{"value": str(uuid.uuid4())}]}
            ),
        ),
        400,
    )
    assert error["scimType"] == "invalidValue"
