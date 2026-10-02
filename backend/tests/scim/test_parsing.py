"""Filter and PATCH parsing (no database)."""

import pytest

from app.scim.errors import ScimError
from app.scim.filters import Condition, parse_filter
from app.scim.patch import as_bool, operations, parse_path
from app.scim.service import (
    GROUP_FILTERS,
    USER_FILTERS,
    GroupData,
    UserData,
    patch_group,
    patch_user,
)


def test_filters() -> None:
    assert parse_filter(None, USER_FILTERS) == []
    assert parse_filter('userName eq "a@b.org"', USER_FILTERS) == [Condition("username", "a@b.org")]
    assert parse_filter(
        'urn:ietf:params:scim:schemas:core:2.0:User:userName EQ "x" and externalId eq "y"',
        USER_FILTERS,
    ) == [Condition("username", "x"), Condition("externalid", "y")]
    assert parse_filter('displayName eq "Quote \\" inside"', GROUP_FILTERS) == [
        Condition("displayname", 'Quote " inside')
    ]
    assert parse_filter('id eq "1" and members[value eq "2"]', GROUP_FILTERS) == [
        Condition("id", "1"),
        Condition("members.value", "2"),
    ]
    assert parse_filter("active eq false", USER_FILTERS) == [Condition("active", False)]


@pytest.mark.parametrize(
    "text",
    [
        'userName co "a"',
        'userName eq "a" or userName eq "b"',
        'not (userName eq "a")',
        "userName pr",
        'title eq "x"',
        'userName eq "unterminated',
        'members[value eq "1"',
        "userName eq",
    ],
)
def test_unsupported_filters(text: str) -> None:
    with pytest.raises(ScimError) as error:
        parse_filter(text, USER_FILTERS | {"members.value"})
    assert error.value.scim_type == "invalidFilter"


def test_paths() -> None:
    assert parse_path("active") == ("active", None, None)
    assert parse_path("name.givenName") == ("name.givenname", None, None)
    assert parse_path('emails[type eq "work"].value') == (
        "emails",
        Condition("type", "work"),
        "value",
    )
    assert parse_path('members[value eq "abc"]') == ("members", Condition("value", "abc"), None)
    assert parse_path("urn:ietf:params:scim:schemas:core:2.0:User:userName") == (
        "username",
        None,
        None,
    )


def test_operations_variants() -> None:
    body = {
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
        "Operations": [
            {"op": "Replace", "path": "active", "value": "False"},
            {
                "op": "replace",
                "value": {
                    "displayName": "New",
                    "name.givenName": "G",
                    "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User": {"x": 1},
                },
            },
        ],
    }
    ops = operations(body)
    assert [(op.op, op.attribute) for op in ops] == [
        ("replace", "active"),
        ("replace", "displayname"),
        ("replace", "name.givenname"),
    ]
    for bad in ({}, {"Operations": []}, {"Operations": [{"op": "move", "path": "a"}]}):
        with pytest.raises(ScimError):
            operations(bad)
    with pytest.raises(ScimError) as no_target:
        operations({"Operations": [{"op": "remove"}]})
    assert no_target.value.scim_type == "noTarget"


def test_booleans() -> None:
    assert as_bool(True) is True
    assert as_bool("False") is False
    assert as_bool("TRUE") is True
    with pytest.raises(ScimError):
        as_bool("no")


def test_patch_user_ignores_unsupported_attributes() -> None:
    data = UserData(user_name="a", email="a@example.org", display_name="A")
    patched = patch_user(
        data,
        operations(
            {
                "Operations": [
                    {"op": "add", "path": "phoneNumbers", "value": [{"value": "123"}]},
                    {"op": "replace", "path": "emails", "value": [{"value": "B@Example.org"}]},
                    {"op": "remove", "path": "externalId"},
                ]
            }
        ),
    )
    assert patched.email == "b@example.org"
    assert patched.external_id is None
    assert data.email == "a@example.org"  # not changed in place


def test_patch_group_members() -> None:
    first, second = "00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000002"
    data = GroupData(display_name="G", members={first})
    added = patch_group(
        data,
        operations(
            {"Operations": [{"op": "add", "path": "members", "value": [{"value": second}]}]}
        ),
    )
    assert added.members == {first, second}
    replaced = patch_group(
        added,
        operations(
            {"Operations": [{"op": "replace", "path": "members", "value": [{"value": second}]}]}
        ),
    )
    assert replaced.members == {second}
    cleared = patch_group(added, operations({"Operations": [{"op": "remove", "path": "members"}]}))
    assert cleared.members == set()
    with pytest.raises(ScimError):
        patch_group(data, operations({"Operations": [{"op": "remove", "path": "displayName"}]}))
