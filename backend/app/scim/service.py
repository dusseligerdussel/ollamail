"""Provisioning operations behind the SCIM endpoints (#95).

Users
    ``POST /Users`` creates a user (role: the default role of the group → role mapping, else
    ``user``) or takes over an existing account with the same e-mail address that SCIM
    does not manage yet (e.g. created by an earlier login). Each provisioned user has a
    ``scim_users`` row and an identity with provider ``scim`` that carries their groups.
    ``active=false`` deactivates the account and deletes all its sessions in the same
    transaction, so the next request of the person fails (``resolve_session`` checks
    ``is_active``); ``DELETE`` uses the deletion of ``app.privacy.deletion`` (#36), which
    removes the user's own mailboxes but never shared mailboxes. Both are refused with 409 if
    they would remove the last working admin access (``AdminAccessGuard``) resp. the last
    active admin.

Groups
    Group memberships are mirrored into ``auth_identities.groups`` of the ``scim`` identity
    (display name and ``externalId`` of each group). From there the group → role mapping
    (#33, rules for provider ``scim`` or all providers) and the group assignments of shared
    mailboxes (#34) use them like the groups of a login. With the role mapping on, every
    membership change re-evaluates the role of the affected users; the last active admin is
    never demoted.

Every change is written to the audit log in the same transaction, with IDs only.
"""

import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Any

from fastapi import Request
from sqlalchemy import (
    ColumnElement,
    Select,
    delete,
    exists,
    false,
    func,
    insert,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app import audit
from app.auth.admin_access import AdminAccessGuard, other_active_admins
from app.auth.models import LOCAL_PROVIDER, SCIM_PROVIDER, Identity
from app.auth.policy import Rule, get_policy, list_rules, mapped_role, resolve_role
from app.auth.providers import AuthProviderRegistry
from app.auth.provisioning import clean_groups
from app.auth.sessions import revoke_user_sessions
from app.core.config import Settings
from app.core.logging import get_logger
from app.privacy import deletion
from app.privacy.router import UserDeletionRequester, finish_in_background, get_file_stores
from app.scim.errors import ScimError, invalid_value, not_found
from app.scim.filters import Condition, invalid_filter
from app.scim.models import ScimGroup, ScimUser, scim_group_members
from app.scim.patch import Operation, as_bool, as_text, invalid_path
from app.users.models import User, UserRole
from app.users.schemas import normalize_email
from app.users.service import get_user_by_email

log = get_logger(__name__)

USER_FILTERS = frozenset(
    {"id", "username", "externalid", "emails", "emails.value", "displayname", "active"}
)
GROUP_FILTERS = frozenset({"id", "displayname", "externalid", "members", "members.value"})

_MAX_USER_NAME = 320
_MAX_NAME = 255
_MAX_EXTERNAL_ID = 255
_MAX_MEMBERS = 10_000
_SYSTEM = audit.SYSTEM


# -- request data ------------------------------------------------------------------------


@dataclass
class UserData:
    user_name: str
    email: str
    display_name: str
    external_id: str | None = None
    active: bool = True


@dataclass
class GroupData:
    display_name: str
    external_id: str | None = None
    # Member user IDs as sent; validated when stored.
    members: set[str] = field(default_factory=set)


def _object(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise invalid_value("The request body must be a JSON object.")
    return body


def _clip(value: str | None, length: int) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value[:length] if value else None


def _primary(entry: dict[str, Any]) -> bool:
    return entry.get("primary") is True or str(entry.get("primary")).lower() == "true"


def _email_from(emails: Any) -> str | None:
    """The primary, else the work, else the first address of an ``emails`` value."""
    if emails is None:
        return None
    if isinstance(emails, dict):
        emails = [emails]
    if not isinstance(emails, list):
        raise invalid_value("emails must be a list.")
    candidates = [
        entry
        for entry in emails
        if isinstance(entry, dict)
        and isinstance(entry.get("value"), str)
        and entry["value"].strip()
    ]
    for entry in candidates:
        if _primary(entry):
            return str(entry["value"])
    for entry in candidates:
        if str(entry.get("type", "")).lower() == "work":
            return str(entry["value"])
    return str(candidates[0]["value"]) if candidates else None


def _normalized_email(value: str) -> str:
    try:
        return normalize_email(value)
    except ValueError:
        raise invalid_value("The e-mail address is not valid.") from None


def _name_from(body: dict[str, Any]) -> str | None:
    display = body.get("displayName")
    if isinstance(display, str) and display.strip():
        return _clip(display, _MAX_NAME)
    name = body.get("name")
    if isinstance(name, dict):
        formatted = name.get("formatted")
        if isinstance(formatted, str) and formatted.strip():
            return _clip(formatted, _MAX_NAME)
        parts = [name.get(key) for key in ("givenName", "familyName")]
        joined = " ".join(p.strip() for p in parts if isinstance(p, str) and p.strip())
        if joined:
            return _clip(joined, _MAX_NAME)
    return None


def user_data(body: Any) -> UserData:
    """``UserData`` of a ``POST``/``PUT`` body."""
    body = _object(body)
    user_name = as_text(body.get("userName"), _MAX_USER_NAME)
    assert user_name is not None
    raw_email = _email_from(body.get("emails"))
    if raw_email is None and "@" in user_name:
        raw_email = user_name
    if raw_email is None:
        raise invalid_value("An e-mail address is required.")
    email = _normalized_email(raw_email)
    active = body.get("active")
    return UserData(
        user_name=user_name,
        email=email,
        display_name=_name_from(body) or email.partition("@")[0],
        external_id=as_text(body.get("externalId"), _MAX_EXTERNAL_ID, required=False),
        active=True if active is None else as_bool(active),
    )


def current_user_data(user: User, scim_user: ScimUser) -> UserData:
    return UserData(
        user_name=scim_user.user_name,
        email=user.email,
        display_name=user.display_name,
        external_id=scim_user.external_id,
        active=user.is_active,
    )


def _patched_email(operation: Operation) -> str | None:
    value = operation.value
    if operation.selector is not None or operation.attribute == "emails.value":
        # emails[type eq "work"].value = "..." (Entra ID) or emails.value = "..."
        if operation.sub_attribute not in (None, "value"):
            return None
        if isinstance(value, list):
            return _email_from(value)
        return value if isinstance(value, str) and value.strip() else None
    return _email_from(value)


def patch_user(data: UserData, operations: Iterable[Operation]) -> UserData:
    """``data`` with the operations applied. Unsupported attributes are ignored."""
    data = replace(data)
    for operation in operations:
        attribute, remove = operation.attribute, operation.op == "remove"
        if attribute == "active":
            if not remove:
                data.active = as_bool(operation.value)
        elif attribute == "username":
            if remove:
                raise ScimError(400, "userName is required.", "mutability")
            user_name = as_text(operation.value, _MAX_USER_NAME)
            assert user_name is not None
            data.user_name = user_name
        elif attribute == "displayname":
            name = _clip(operation.value, _MAX_NAME) if isinstance(operation.value, str) else None
            if not remove and name:
                data.display_name = name
        elif attribute == "externalid":
            data.external_id = (
                None if remove else as_text(operation.value, _MAX_EXTERNAL_ID, required=False)
            )
        elif attribute in {"emails", "emails.value"} and not remove:
            email = _patched_email(operation)
            if email:
                data.email = _normalized_email(email)
        # Other attributes (name parts, phone numbers, extension ...) are not stored.
    return data


def _member_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list) or len(value) > _MAX_MEMBERS:
        raise invalid_value("members must be a list.")
    result = []
    for entry in value:
        member = entry.get("value") if isinstance(entry, dict) else None
        if not isinstance(member, str) or not member.strip():
            raise invalid_value("Each member needs a value.")
        result.append(_member_key(member))
    return result


def _member_key(value: str) -> str:
    try:
        return str(uuid.UUID(value.strip()))
    except ValueError:
        return value.strip()


def group_data(body: Any) -> GroupData:
    body = _object(body)
    name = as_text(body.get("displayName"), _MAX_NAME)
    assert name is not None
    return GroupData(
        display_name=name,
        external_id=as_text(body.get("externalId"), _MAX_EXTERNAL_ID, required=False),
        members=set(_member_values(body.get("members"))),
    )


def patch_group(data: GroupData, operations: Iterable[Operation]) -> GroupData:
    data = replace(data, members=set(data.members))
    for operation in operations:
        attribute, op = operation.attribute, operation.op
        if attribute == "displayname":
            if op == "remove":
                raise ScimError(400, "displayName is required.", "mutability")
            name = as_text(operation.value, _MAX_NAME)
            assert name is not None
            data.display_name = name
        elif attribute == "externalid":
            data.external_id = (
                None
                if op == "remove"
                else as_text(operation.value, _MAX_EXTERNAL_ID, required=False)
            )
        elif attribute == "members":
            selector = operation.selector
            if selector is not None:
                # members[value eq "<id>"]: Entra ID and Okta use it to remove one member.
                if selector.attribute != "value" or not isinstance(selector.value, str):
                    raise invalid_path()
                if op != "remove":
                    raise invalid_path("Only remove is supported with a member filter.")
                data.members.discard(_member_key(selector.value))
            elif op == "add":
                data.members.update(_member_values(operation.value))
            elif op == "replace":
                data.members = set(_member_values(operation.value))
            elif operation.value is None:
                data.members.clear()
            else:
                data.members.difference_update(_member_values(operation.value))
        # ``id`` (sent by Okta) and unknown attributes are ignored.
    return data


# -- queries -----------------------------------------------------------------------------


def _uuid(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if value is not None else None
    except ValueError:
        return None


def _text(condition: Condition) -> str:
    if not isinstance(condition.value, str):
        raise invalid_filter("The filter value must be a string.")
    return condition.value


def _id_clause(
    column: InstrumentedAttribute[uuid.UUID], condition: Condition
) -> ColumnElement[bool]:
    value = _uuid(_text(condition))
    return column == value if value is not None else false()


def _user_clause(condition: Condition) -> ColumnElement[bool]:
    attribute = condition.attribute
    if attribute == "active":
        if not isinstance(condition.value, bool):
            raise invalid_filter("The filter value must be a boolean.")
        return User.is_active.is_(condition.value)
    value = _text(condition)
    if attribute == "id":
        return _id_clause(User.id, condition)
    if attribute == "username":
        return func.lower(ScimUser.user_name) == value.lower()
    if attribute == "externalid":
        return ScimUser.external_id == value
    if attribute in {"emails", "emails.value"}:
        return func.lower(User.email) == value.strip().lower()
    return User.display_name == value


def _group_clause(condition: Condition) -> ColumnElement[bool]:
    attribute, value = condition.attribute, _text(condition)
    if attribute == "id":
        return _id_clause(ScimGroup.id, condition)
    if attribute == "displayname":
        return func.lower(ScimGroup.display_name) == value.lower()
    if attribute == "externalid":
        return ScimGroup.external_id == value
    member = _uuid(value)
    if member is None:
        return false()
    return exists(
        select(scim_group_members.c.user_id).where(
            scim_group_members.c.group_id == ScimGroup.id,
            scim_group_members.c.user_id == member,
        )
    )


async def _page(
    db: AsyncSession,
    query: Select[*tuple[Any, ...]],
    start_index: int,
    count: int,
) -> tuple[int, list[Any]]:
    total = await db.scalar(select(func.count()).select_from(query.subquery())) or 0
    if count == 0:
        return total, []
    rows = await db.execute(query.offset(start_index - 1).limit(count))
    return total, list(rows.all())


async def list_users(
    db: AsyncSession, conditions: list[Condition], start_index: int, count: int
) -> tuple[int, list[tuple[User, ScimUser]]]:
    query = (
        select(User, ScimUser)
        .join(ScimUser, ScimUser.user_id == User.id)
        .where(*[_user_clause(c) for c in conditions])
        .order_by(User.id)
    )
    total, rows = await _page(db, query, start_index, count)
    return total, [(row[0], row[1]) for row in rows]


async def list_groups(
    db: AsyncSession, conditions: list[Condition], start_index: int, count: int
) -> tuple[int, list[ScimGroup]]:
    query = select(ScimGroup).where(*[_group_clause(c) for c in conditions]).order_by(ScimGroup.id)
    total, rows = await _page(db, query, start_index, count)
    return total, [row[0] for row in rows]


async def get_user(db: AsyncSession, user_id: str) -> tuple[User, ScimUser]:
    parsed = _uuid(user_id)
    row = None
    if parsed is not None:
        row = (
            await db.execute(
                select(User, ScimUser)
                .join(ScimUser, ScimUser.user_id == User.id)
                .where(User.id == parsed)
            )
        ).one_or_none()
    if row is None:
        raise not_found("User")
    return row[0], row[1]


async def get_group(db: AsyncSession, group_id: str) -> ScimGroup:
    parsed = _uuid(group_id)
    group = await db.get(ScimGroup, parsed) if parsed is not None else None
    if group is None:
        raise not_found("Group")
    return group


async def groups_of(
    db: AsyncSession, user_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, list[ScimGroup]]:
    ids = list(user_ids)
    result: dict[uuid.UUID, list[ScimGroup]] = defaultdict(list)
    if not ids:
        return result
    rows = await db.execute(
        select(scim_group_members.c.user_id, ScimGroup)
        .join(ScimGroup, ScimGroup.id == scim_group_members.c.group_id)
        .where(scim_group_members.c.user_id.in_(ids))
        .order_by(ScimGroup.display_name, ScimGroup.id)
    )
    for user_id, group in rows:
        result[user_id].append(group)
    return result


async def members_of(
    db: AsyncSession, group_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, list[tuple[User, ScimUser]]]:
    ids = list(group_ids)
    result: dict[uuid.UUID, list[tuple[User, ScimUser]]] = defaultdict(list)
    if not ids:
        return result
    rows = await db.execute(
        select(scim_group_members.c.group_id, User, ScimUser)
        .join(User, User.id == scim_group_members.c.user_id)
        .join(ScimUser, ScimUser.user_id == User.id)
        .where(scim_group_members.c.group_id.in_(ids))
        .order_by(User.id)
    )
    for group_id, user, scim_user in rows:
        result[group_id].append((user, scim_user))
    return result


async def member_ids(db: AsyncSession, group_id: uuid.UUID) -> set[uuid.UUID]:
    return set(
        await db.scalars(
            select(scim_group_members.c.user_id).where(scim_group_members.c.group_id == group_id)
        )
    )


# -- users -------------------------------------------------------------------------------


def _registry(request: Request) -> AuthProviderRegistry:
    registry: AuthProviderRegistry = request.app.state.auth_providers
    return registry


def _conflict(detail: str) -> ScimError:
    return ScimError(409, detail, "uniqueness")


async def _check_user_name(db: AsyncSession, user_name: str, user_id: uuid.UUID | None) -> None:
    query = select(ScimUser.user_id).where(func.lower(ScimUser.user_name) == user_name.lower())
    if user_id is not None:
        query = query.where(ScimUser.user_id != user_id)
    if await db.scalar(query) is not None:
        raise _conflict("A user with this userName already exists.")


async def create_user(db: AsyncSession, request: Request, data: UserData) -> tuple[User, ScimUser]:
    """Create or take over the user for ``data`` and commit."""
    await _check_user_name(db, data.user_name, None)
    existing = await get_user_by_email(db, data.email)
    try:
        async with db.begin_nested():
            if existing is not None:
                managed = await db.scalar(
                    select(ScimUser.id).where(ScimUser.user_id == existing.id)
                )
                if managed is not None:
                    raise _conflict("A user with this e-mail address already exists.")
                user = existing
            else:
                role = await resolve_role(db, SCIM_PROVIDER, [], None) or UserRole.USER
                user = User(
                    email=data.email,
                    display_name=data.display_name,
                    role=role,
                    is_active=data.active,
                )
                db.add(user)
                await db.flush()
            scim_user = ScimUser(
                user_id=user.id, user_name=data.user_name, external_id=data.external_id
            )
            db.add(scim_user)
            db.add(
                Identity(user_id=user.id, provider=SCIM_PROVIDER, subject=str(user.id), groups=[])
            )
            await db.flush()
    except IntegrityError:
        # A concurrent request created the same userName or address.
        raise _conflict("A user with this userName or e-mail address already exists.") from None
    target = audit.Target.of(audit.TargetType.USER, user.id)
    if existing is None:
        await audit.record(
            db,
            _SYSTEM,
            audit.AuditAction.USER_CREATED,
            target,
            {"role": str(user.role), "via": "scim", "active": user.is_active},
        )
        log.info("scim_user_created", user_id=user.id, role=user.role)
    else:
        await audit.record(
            db, _SYSTEM, audit.AuditAction.USER_UPDATED, target, {"via": "scim", "linked": True}
        )
        log.info("scim_user_linked", user_id=user.id)
        await _apply_user(db, request, user, scim_user, data)
    await db.commit()
    await db.refresh(user)
    await db.refresh(scim_user)
    return user, scim_user


async def update_user(
    db: AsyncSession, request: Request, user: User, scim_user: ScimUser, data: UserData
) -> tuple[User, ScimUser]:
    """Apply ``data`` (``PUT`` or patched state) and commit."""
    await _apply_user(db, request, user, scim_user, data)
    await db.commit()
    await db.refresh(user)
    await db.refresh(scim_user)
    return user, scim_user


async def _apply_user(
    db: AsyncSession, request: Request, user: User, scim_user: ScimUser, data: UserData
) -> None:
    target = audit.Target.of(audit.TargetType.USER, user.id)
    guard = None
    if user.is_active and not data.active:
        guard = await AdminAccessGuard.start(db, _registry(request))
    changed: list[str] = []
    if scim_user.user_name != data.user_name:
        await _check_user_name(db, data.user_name, user.id)
        scim_user.user_name = data.user_name
        changed.append("user_name")
    if scim_user.external_id != data.external_id:
        scim_user.external_id = data.external_id
        changed.append("external_id")
    if user.email != data.email:
        other = await get_user_by_email(db, data.email)
        if other is not None and other.id != user.id:
            raise _conflict("A user with this e-mail address already exists.")
        user.email = data.email
        changed.append("email")
    if user.display_name != data.display_name:
        user.display_name = data.display_name
        changed.append("display_name")
    if changed:
        await audit.record(
            db,
            _SYSTEM,
            audit.AuditAction.USER_UPDATED,
            target,
            {"via": "scim", "fields": ",".join(changed)},
        )
    if data.active != user.is_active:
        user.is_active = data.active
        if data.active:
            await audit.record(
                db, _SYSTEM, audit.AuditAction.USER_REACTIVATED, target, {"via": "scim"}
            )
            log.info("scim_user_reactivated", user_id=user.id)
        else:
            sessions = await revoke_user_sessions(db, user.id)
            assert guard is not None
            await guard.check()
            await audit.record(
                db,
                _SYSTEM,
                audit.AuditAction.USER_DEACTIVATED,
                target,
                {"via": "scim", "sessions": sessions},
            )
            log.info("scim_user_deactivated", user_id=user.id, sessions=sessions)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise _conflict("A user with this userName or e-mail address already exists.") from None


async def delete_user(
    db: AsyncSession, request: Request, user_id: str, finisher: UserDeletionRequester
) -> None:
    """Delete a provisioned user with all their data (``app.privacy.deletion``)."""
    user, _ = await get_user(db, user_id)
    settings: Settings = request.app.state.settings
    guard = await AdminAccessGuard.start(db, _registry(request))
    result = await deletion.delete_user(
        db,
        user.id,
        get_file_stores(settings),
        actor=_SYSTEM,
        via="scim",
        access_guard=guard,
    )
    if result is not None:
        await finish_in_background(finisher, result)


# -- groups ------------------------------------------------------------------------------


async def _resolve_members(db: AsyncSession, values: Iterable[str]) -> set[uuid.UUID]:
    """User IDs for member values; 400 if one is not a provisioned user."""
    wanted = set(values)
    if not wanted:
        return set()
    ids = {parsed for value in wanted if (parsed := _uuid(value)) is not None}
    known = set(await db.scalars(select(ScimUser.user_id).where(ScimUser.user_id.in_(ids))))
    if len(known) != len(wanted):
        raise invalid_value("A member is not a provisioned user.")
    return known


async def create_group(db: AsyncSession, data: GroupData) -> ScimGroup:
    members = await _resolve_members(db, data.members)
    group = ScimGroup(display_name=data.display_name, external_id=data.external_id)
    db.add(group)
    await db.flush()
    await audit.record(
        db,
        _SYSTEM,
        audit.AuditAction.GROUP_CREATED,
        audit.Target.of(audit.TargetType.GROUP, group.id),
        {"via": "scim", "members": len(members)},
    )
    await _change_members(db, group.id, added=members, removed=set())
    await refresh_user_groups(db, members)
    await db.commit()
    await db.refresh(group)
    log.info("scim_group_created", group_id=group.id, members=len(members))
    return group


async def update_group(db: AsyncSession, group: ScimGroup, data: GroupData) -> ScimGroup:
    """Apply ``data`` (``PUT`` or patched state) and commit."""
    before = await member_ids(db, group.id)
    added = await _resolve_members(db, data.members - {str(i) for i in before})
    removed = {user_id for user_id in before if str(user_id) not in data.members}
    changed: list[str] = []
    if group.display_name != data.display_name:
        group.display_name = data.display_name
        changed.append("display_name")
    if group.external_id != data.external_id:
        group.external_id = data.external_id
        changed.append("external_id")
    if changed:
        await audit.record(
            db,
            _SYSTEM,
            audit.AuditAction.GROUP_UPDATED,
            audit.Target.of(audit.TargetType.GROUP, group.id),
            {"via": "scim", "fields": ",".join(changed)},
        )
    await _change_members(db, group.id, added=added, removed=removed)
    # A new name changes the groups of every member, else only of the changed ones.
    await refresh_user_groups(db, (before | added) if changed else (added | removed))
    await db.commit()
    await db.refresh(group)
    if changed or added or removed:
        log.info(
            "scim_group_updated",
            group_id=group.id,
            changed=changed,
            added=len(added),
            removed=len(removed),
        )
    return group


async def delete_group(db: AsyncSession, group: ScimGroup) -> None:
    members = await member_ids(db, group.id)
    group_id = group.id
    await db.delete(group)
    await db.flush()
    await audit.record(
        db,
        _SYSTEM,
        audit.AuditAction.GROUP_DELETED,
        audit.Target.of(audit.TargetType.GROUP, group_id),
        {"via": "scim", "members": len(members)},
    )
    await refresh_user_groups(db, members)
    await db.commit()
    log.info("scim_group_deleted", group_id=group_id, members=len(members))


async def _change_members(
    db: AsyncSession, group_id: uuid.UUID, *, added: set[uuid.UUID], removed: set[uuid.UUID]
) -> None:
    if added:
        await db.execute(
            insert(scim_group_members),
            [{"group_id": group_id, "user_id": user_id} for user_id in sorted(added)],
        )
    if removed:
        await db.execute(
            delete(scim_group_members).where(
                scim_group_members.c.group_id == group_id,
                scim_group_members.c.user_id.in_(removed),
            )
        )
    for action, user_ids in (
        (audit.AuditAction.GROUP_MEMBER_ADDED, added),
        (audit.AuditAction.GROUP_MEMBER_REMOVED, removed),
    ):
        for user_id in sorted(user_ids):
            await audit.record(
                db,
                _SYSTEM,
                action,
                audit.Target.of(audit.TargetType.USER, user_id),
                {"via": "scim", "group_id": str(group_id)},
            )


async def refresh_user_groups(db: AsyncSession, user_ids: Iterable[uuid.UUID]) -> None:
    """Mirror the SCIM groups of ``user_ids`` into their ``scim`` identity and re-evaluate
    their roles (role mapping on)."""
    ids = set(user_ids)
    if not ids:
        return
    names: dict[uuid.UUID, list[str]] = defaultdict(list)
    rows = await db.execute(
        select(scim_group_members.c.user_id, ScimGroup.display_name, ScimGroup.external_id)
        .join(ScimGroup, ScimGroup.id == scim_group_members.c.group_id)
        .where(scim_group_members.c.user_id.in_(ids))
    )
    for user_id, display_name, external_id in rows:
        names[user_id].append(display_name)
        if external_id:
            names[user_id].append(external_id)
    identities = await db.scalars(
        select(Identity).where(Identity.user_id.in_(ids), Identity.provider == SCIM_PROVIDER)
    )
    for identity in identities:
        identity.groups = clean_groups(names.get(identity.user_id, []))
    await db.flush()
    await _sync_roles(db, ids)


async def _sync_roles(db: AsyncSession, user_ids: set[uuid.UUID]) -> None:
    policy = await get_policy(db)
    if not policy.role_mapping_enabled:
        return
    rules = [Rule(r.group, r.provider, r.role) for r in await list_rules(db)]
    groups: dict[uuid.UUID, list[tuple[str, list[str]]]] = defaultdict(list)
    for user_id, provider, identity_groups in await db.execute(
        select(Identity.user_id, Identity.provider, Identity.groups).where(
            Identity.user_id.in_(user_ids), Identity.provider != LOCAL_PROVIDER
        )
    ):
        groups[user_id].append((provider, list(identity_groups)))
    users = await db.scalars(select(User).where(User.id.in_(user_ids)).order_by(User.id))
    for user in users:
        sources = groups.get(user.id)
        if not sources:
            continue
        (provider, first), *rest = sources
        role = mapped_role(rules, policy.default_role, provider, first, None, rest)
        if role == user.role:
            continue
        if user.role is UserRole.ADMIN and not await other_active_admins(db, user.id):
            log.warning("user_role_sync_skipped", user_id=user.id, reason="last_admin")
            continue
        await audit.record(
            db,
            _SYSTEM,
            audit.AuditAction.USER_ROLE_CHANGED,
            audit.Target.of(audit.TargetType.USER, user.id),
            {"from_role": str(user.role), "to_role": str(role), "provider": SCIM_PROVIDER},
        )
        log.info("user_role_synced", user_id=user.id, role=role, provider=SCIM_PROVIDER)
        user.role = role
    await db.flush()
