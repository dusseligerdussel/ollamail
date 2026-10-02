import csv
import io
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.audit import AuditAction
from app.users.models import User, UserRole
from tests.audit.conftest import audit_rows
from tests.auth.conftest import login, make_local_user

pytestmark = pytest.mark.db


async def _sign_in(client: AsyncClient, db: AsyncSession, role: UserRole) -> User:
    user = await make_local_user(db, f"{role}@example.org", role=role)
    assert (await login(client, user.email)).status_code == 200
    return user


@pytest.mark.parametrize("path", ["/audit/events", "/audit/events/export", "/audit/verify"])
async def test_only_admins_have_access(
    db_client: AsyncClient, db_session: AsyncSession, path: str
) -> None:
    assert (await db_client.get(path)).status_code == 401

    await _sign_in(db_client, db_session, UserRole.USER)

    assert (await db_client.get(path)).status_code == 403
    # A denied export is not recorded as an export.
    assert await audit_rows(db_session, AuditAction.AUDIT_EXPORTED) == []


async def test_list_newest_first_with_names(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin = await _sign_in(db_client, db_session, UserRole.ADMIN)
    other = await make_local_user(db_session, "max@example.org")
    await audit.record(
        db_session,
        audit.Actor.user(admin.id),
        AuditAction.USER_ROLE_CHANGED,
        audit.Target.of(audit.TargetType.USER, other.id),
        {"role": "admin"},
    )
    await db_session.commit()

    page = (await db_client.get("/audit/events")).json()

    newest, login_event = page["items"]
    assert page["next_before"] is None
    assert newest["action"] == "user.role_changed"
    assert newest["actor_name"] == admin.display_name
    assert newest["target_id"] == str(other.id)
    assert newest["target_name"] == other.display_name
    assert newest["details"] == {"role": "admin"}
    assert login_event["action"] == "auth.login_succeeded"
    assert newest["id"] > login_event["id"]
    assert "hash" not in newest


async def test_filters_and_pagination(db_client: AsyncClient, db_session: AsyncSession) -> None:
    admin = await _sign_in(db_client, db_session, UserRole.ADMIN)
    for _ in range(5):
        await audit.record(db_session, audit.ANONYMOUS, AuditAction.LOGIN_FAILED)
    await db_session.commit()

    first = (await db_client.get("/audit/events?action=auth.login_failed&limit=3")).json()
    second = (
        await db_client.get(
            f"/audit/events?action=auth.login_failed&limit=3&before={first['next_before']}"
        )
    ).json()
    by_actor = (await db_client.get(f"/audit/events?actor_id={admin.id}")).json()
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    since_future = await db_client.get("/audit/events", params={"since": future})
    until_future = await db_client.get("/audit/events", params={"until": future})

    assert len(first["items"]) == 3
    assert len(second["items"]) == 2
    assert second["next_before"] is None
    ids = [item["id"] for item in first["items"] + second["items"]]
    assert ids == sorted(ids, reverse=True)
    assert [item["action"] for item in by_actor["items"]] == ["auth.login_succeeded"]
    assert since_future.json()["items"] == []
    assert len(until_future.json()["items"]) == 6
    assert (await db_client.get("/audit/events?action=nope")).status_code == 422
    assert (await db_client.get("/audit/events?limit=500")).status_code == 422


async def test_csv_export(db_client: AsyncClient, db_session: AsyncSession) -> None:
    admin = await _sign_in(db_client, db_session, UserRole.ADMIN)
    # Display names are user input: no spreadsheet formulas in the export.
    admin.display_name = "=HYPERLINK()"
    target = uuid.uuid4()
    await audit.record(
        db_session,
        audit.Actor.user(admin.id),
        AuditAction.MAILBOX_DELETED,
        audit.Target.of(audit.TargetType.MAILBOX, target),
        {"count": 1},
    )
    await db_session.commit()

    response = await db_client.get("/audit/events/export?target_type=mailbox")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"].startswith('attachment; filename="audit-log-')
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows) == 1
    assert rows[0]["action"] == "mailbox.deleted"
    assert rows[0]["target_id"] == str(target)
    assert rows[0]["actor_name"] == "'=HYPERLINK()"
    assert rows[0]["details"] == '{"count":1}'
    (exported,) = await audit_rows(db_session, AuditAction.AUDIT_EXPORTED)
    assert exported.actor_id == admin.id


async def test_verify(db_client: AsyncClient, db_session: AsyncSession) -> None:
    await _sign_in(db_client, db_session, UserRole.ADMIN)

    status = (await db_client.get("/audit/verify")).json()

    assert status["valid"] is True
    assert status["checked"] >= 1
    assert status["first_invalid_id"] is None
