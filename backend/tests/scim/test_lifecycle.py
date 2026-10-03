"""What provisioning does to accounts: sessions and access end at once on deactivation,
deletion goes through the deletion concept (#36) and spares shared mailboxes, groups feed
the role mapping (#33) and shared mailbox assignments (#34), and logins link to users
created by SCIM. All names and addresses are invented (docs/PRIVACY.md)."""

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.models import audit_events
from app.auth.models import AuthPolicy, AuthSession, Identity, RoleMappingRule
from app.auth.providers import VerifiedIdentity
from app.auth.provisioning import (
    ProvisioningError,
    ProvisioningErrorCode,
    ProvisioningPolicy,
    provision_user,
)
from app.auth.sessions import SESSION_COOKIE, create_session
from app.mail.access import can_read
from app.mail.models import Mailbox, MailboxAssignment, MailboxType
from app.mail.storage import AttachmentStorage
from app.users.models import User, UserRole
from tests.auth.conftest import make_local_user
from tests.conftest import api_client
from tests.privacy.conftest import finish_user_deletion
from tests.scim.conftest import ok, patch_body, scim_config

pytestmark = pytest.mark.db

ANNA = {
    "userName": "anna.beispiel@example.org",
    "emails": [{"value": "anna.beispiel@example.org", "type": "work", "primary": True}],
    "displayName": "Anna Beispiel",
    "externalId": "anna-ext",
    "active": True,
}


async def _create(idp: AsyncClient, body: dict[str, object] = ANNA) -> uuid.UUID:
    return uuid.UUID(ok(await idp.post("/Users", json=body), 201)["id"])


async def _group(
    idp: AsyncClient, name: str, *members: uuid.UUID, external_id: str | None = None
) -> str:
    body: dict[str, object] = {
        "displayName": name,
        "members": [{"value": str(member)} for member in members],
    }
    if external_id:
        body["externalId"] = external_id
    return str(ok(await idp.post("/Groups", json=body), 201)["id"])


@pytest.fixture
async def browser(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with api_client(app) as client:
        yield client


async def _sign_in(
    app: FastAPI, session: AsyncSession, client: AsyncClient, user_id: uuid.UUID
) -> None:
    """A session as an OIDC login would create it (SCIM users have no password)."""
    user = await session.get(User, user_id)
    assert user is not None
    token = await create_session(
        session, app.state.settings.auth, user, provider="oidc:entra", user_agent="test"
    )
    await session.commit()
    client.cookies.set(SESSION_COOKIE, token)


async def _shared_mailbox(session: AsyncSession, group: str) -> uuid.UUID:
    mailbox = Mailbox(
        type=MailboxType.IMAP,
        display_name="Support",
        address="support@example.org",
        owner_user_id=None,
        is_shared=True,
    )
    session.add(mailbox)
    await session.flush()
    session.add(MailboxAssignment(mailbox_id=mailbox.id, group_name=group, provider="scim"))
    await session.commit()
    return mailbox.id


async def test_deactivated_user_loses_sessions_and_access_at_once(
    app: FastAPI, idp: AsyncClient, browser: AsyncClient, db_session: AsyncSession
) -> None:
    user_id = await _create(idp)
    await _group(idp, "Support-Team", user_id)
    mailbox_id = await _shared_mailbox(db_session, "support-team")
    await _sign_in(app, db_session, browser, user_id)
    async with api_client(app) as phone:  # a second device
        await _sign_in(app, db_session, phone, user_id)

    assert (await browser.get("/auth/me")).status_code == 200
    assert await can_read(db_session, user_id, mailbox_id)
    assert (await browser.get(f"/mailboxes/{mailbox_id}")).status_code == 200

    ok(
        await idp.patch(
            f"/Users/{user_id}",
            json=patch_body({"op": "Replace", "path": "active", "value": False}),
        )
    )

    sessions = await db_session.scalar(
        select(func.count()).select_from(AuthSession).where(AuthSession.user_id == user_id)
    )
    assert sessions == 0
    assert (await browser.get("/auth/me")).status_code == 401
    assert (await browser.get(f"/mailboxes/{mailbox_id}")).status_code == 401
    # No new login either.
    with pytest.raises(ProvisioningError) as refused:
        await provision_user(
            db_session,
            VerifiedIdentity(
                provider="oidc:entra",
                subject="anna-oid",
                email="anna.beispiel@example.org",
                email_verified=True,
            ),
            ProvisioningPolicy(link_by_email=True),
        )
    assert refused.value.code is ProvisioningErrorCode.INACTIVE
    deactivated = await db_session.scalar(
        select(audit_events.c.details).where(
            audit_events.c.action == "user.deactivated",
            audit_events.c.target_id == str(user_id),
        )
    )
    assert deactivated == {"via": "scim", "sessions": 2}


async def test_delete_uses_deletion_concept_and_keeps_shared_mailboxes(
    app: FastAPI, idp: AsyncClient, db_session: AsyncSession, tmp_path: Path
) -> None:
    user_id = await _create(idp)
    await _group(idp, "Support-Team", user_id)
    shared_id = await _shared_mailbox(db_session, "Support-Team")
    own = Mailbox(
        type=MailboxType.IMAP,
        display_name="Anna",
        address="anna.beispiel@example.org",
        owner_user_id=user_id,
    )
    db_session.add(own)
    await db_session.commit()
    own_id = own.id

    ok(await idp.delete(f"/Users/{user_id}"), 204)

    # Gone for the IdP at once; the mailbox is removed in the background (#177).
    assert (await idp.get(f"/Users/{user_id}")).status_code == 404
    assert app.state.user_deletion_requests == [user_id]
    await finish_user_deletion(db_session, user_id, AttachmentStorage(tmp_path), tmp_path)
    db_session.expunge_all()
    assert await db_session.get(User, user_id) is None
    assert await db_session.get(Mailbox, own_id) is None
    shared = await db_session.get(Mailbox, shared_id)
    assert shared is not None and shared.is_shared
    details = await db_session.scalar(
        select(audit_events.c.details).where(
            audit_events.c.action == "user.deleted", audit_events.c.target_id == str(user_id)
        )
    )
    assert details == {"via": "scim", "mailboxes": 1}


async def test_last_admin_is_protected(
    idp: AsyncClient, db_session: AsyncSession, admin: User
) -> None:
    # The only admin was created locally and is taken over by SCIM (same address).
    body = {**ANNA, "userName": admin.email, "emails": [{"value": admin.email, "primary": True}]}
    user_id = await _create(idp, body)
    assert user_id == admin.id

    error = ok(
        await idp.patch(
            f"/Users/{user_id}",
            json=patch_body({"op": "replace", "value": {"active": False}}),
        ),
        409,
    )
    assert error["status"] == "409"
    assert ok(await idp.delete(f"/Users/{user_id}"), 409)["status"] == "409"
    db_session.expunge_all()
    user = await db_session.get(User, user_id)
    assert user is not None and user.is_active


async def test_existing_account_is_taken_over(idp: AsyncClient, db_session: AsyncSession) -> None:
    local = await make_local_user(db_session, "anna.beispiel@example.org")
    user_id = await _create(idp)
    assert user_id == local.id
    await db_session.refresh(local)
    assert local.display_name == "Anna Beispiel"
    # A second SCIM user with the same address is a conflict.
    body = {**ANNA, "userName": "anna2", "externalId": "x"}
    assert ok(await idp.post("/Users", json=body), 409)["scimType"] == "uniqueness"


async def test_groups_drive_the_role_mapping(
    idp: AsyncClient, db_session: AsyncSession, admin: User
) -> None:
    db_session.add(
        AuthPolicy(local_login_enabled=True, role_mapping_enabled=True, default_role=UserRole.USER)
    )
    db_session.add(RoleMappingRule(group="0b7c-admins", provider="scim", role=UserRole.ADMIN))
    await db_session.commit()

    user_id = await _create(idp)
    user = await db_session.get(User, user_id)
    assert user is not None and user.role is UserRole.USER

    # Rules may name the group's externalId (Entra object ID) or its display name.
    group_id = await _group(idp, "Admins", user_id, external_id="0b7c-admins")
    await db_session.refresh(user)
    assert user.role is UserRole.ADMIN

    # A login without groups keeps the role SCIM's groups give.
    result = await provision_user(
        db_session,
        VerifiedIdentity(
            provider="oidc:entra",
            subject="anna-oid",
            email="anna.beispiel@example.org",
            email_verified=True,
        ),
        ProvisioningPolicy(link_by_email=True),
    )
    assert result.linked and result.user.role is UserRole.ADMIN

    ok(
        await idp.patch(
            f"/Groups/{group_id}",
            json=patch_body({"op": "remove", "path": f'members[value eq "{user_id}"]'}),
        ),
        204,
    )
    await db_session.refresh(user)
    assert user.role is UserRole.USER
    changes = list(
        await db_session.scalars(
            select(audit_events.c.details).where(
                audit_events.c.action == "user.role_changed",
                audit_events.c.target_id == str(user_id),
            )
        )
    )
    assert [c["to_role"] for c in changes] == ["admin", "user"]


async def test_login_links_to_scim_user_only_for_listed_providers(
    idp: AsyncClient, db_session: AsyncSession
) -> None:
    user_id = await _create(idp)
    identity = VerifiedIdentity(
        provider="oidc:entra",
        subject="anna-oid",
        email="anna.beispiel@example.org",
        email_verified=True,
    )
    with pytest.raises(ProvisioningError) as refused:
        await provision_user(db_session, identity, ProvisioningPolicy())
    assert refused.value.code is ProvisioningErrorCode.EMAIL_CONFLICT

    config = await scim_config(db_session)
    config.link_providers = ["oidc:entra"]
    await db_session.commit()

    # Unverified addresses never link.
    unverified = VerifiedIdentity(
        provider="oidc:entra", subject="anna-oid", email="anna.beispiel@example.org"
    )
    with pytest.raises(ProvisioningError):
        await provision_user(db_session, unverified, ProvisioningPolicy())
    # Another provider does not either.
    other = VerifiedIdentity(
        provider="github",
        subject="42",
        email="anna.beispiel@example.org",
        email_verified=True,
    )
    with pytest.raises(ProvisioningError):
        await provision_user(db_session, other, ProvisioningPolicy())

    result = await provision_user(db_session, identity, ProvisioningPolicy())
    assert result.linked and result.user.id == user_id
    providers = set(
        await db_session.scalars(select(Identity.provider).where(Identity.user_id == user_id))
    )
    assert providers == {"scim", "oidc:entra"}
