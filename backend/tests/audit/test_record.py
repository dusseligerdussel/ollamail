import uuid

import pytest
from sqlalchemy import delete, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.audit.models import audit_events
from app.audit.service import AuditDetailsError, _clean_details, verify_chain
from app.core.config import Settings
from tests.audit.conftest import audit_rows


@pytest.mark.parametrize(
    "details",
    [
        {"email": "x"},
        {"subject": "x"},
        {"sender_address": "x"},
        {"to": "x"},
        {"reason": "erika@example.org"},
        {"reason": "two\nlines"},
        {"reason": "x" * 200},
        {"nested": {"a": 1}},
        {"items": [1, 2]},
        {"ratio": 0.5},
        {"Bad-Key": 1},
        {f"k{i}": i for i in range(17)},
    ],
)
def test_details_reject_content_and_addresses(details: dict[str, object]) -> None:
    with pytest.raises(AuditDetailsError):
        _clean_details(details)


def test_details_accept_ids_counts_flags_and_codes() -> None:
    mailbox_id = uuid.uuid4()

    cleaned = _clean_details(
        {"mailbox_id": mailbox_id, "count": 3, "current": True, "reason": "locked", "x": None}
    )

    assert cleaned == {
        "mailbox_id": str(mailbox_id),
        "count": 3,
        "current": True,
        "reason": "locked",
        "x": None,
    }


@pytest.mark.db
async def test_record_appends_a_hash_chain(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    await audit.record(db_session, audit.Actor.user(user_id), audit.AuditAction.LOGOUT)
    await audit.record(
        db_session,
        audit.SYSTEM,
        audit.AuditAction.MAILBOX_DELETED,
        audit.Target.of(audit.TargetType.MAILBOX, user_id),
        {"count": 2},
    )

    first, second = await audit_rows(db_session)

    assert first.actor_kind == "user"
    assert first.actor_id == user_id
    assert second.actor_kind == "system"
    assert second.actor_id is None
    assert second.target_type == "mailbox"
    assert second.target_id == str(user_id)
    assert second.details == {"count": 2}
    assert second.prev_hash == first.hash
    assert second.id > first.id
    check = await verify_chain(db_session)
    assert check.valid
    assert check.checked >= 2


@pytest.mark.db
@pytest.mark.parametrize(
    "statement",
    [
        update(audit_events).values(action="auth.login_succeeded"),
        delete(audit_events),
    ],
    ids=["update", "delete"],
)
async def test_table_is_append_only(db_session: AsyncSession, statement: object) -> None:
    await audit.record(db_session, audit.ANONYMOUS, audit.AuditAction.LOGIN_FAILED)
    await db_session.commit()

    with pytest.raises(DBAPIError, match="append-only"):
        async with db_session.begin_nested():
            await db_session.execute(statement)  # type: ignore[call-overload]


@pytest.mark.db
async def test_truncate_is_rejected(db_session: AsyncSession) -> None:
    with pytest.raises(DBAPIError, match="append-only"):
        async with db_session.begin_nested():
            await db_session.execute(text("TRUNCATE audit_events"))


@pytest.mark.db
async def test_verify_detects_a_changed_row(db_session: AsyncSession) -> None:
    for _ in range(3):
        await audit.record(db_session, audit.ANONYMOUS, audit.AuditAction.LOGIN_FAILED)
    rows = await audit_rows(db_session)
    middle = rows[-2]

    # Bypass the trigger as a superuser would (only possible with database access).
    await db_session.execute(text("SET LOCAL session_replication_role = replica"))
    await db_session.execute(
        update(audit_events).where(audit_events.c.id == middle.id).values(actor_kind="system")
    )
    await db_session.execute(text("SET LOCAL session_replication_role = origin"))

    check = await verify_chain(db_session)
    assert not check.valid
    assert check.first_invalid_id == middle.id


@pytest.mark.db
async def test_verify_detects_a_removed_row(db_session: AsyncSession) -> None:
    for _ in range(3):
        await audit.record(db_session, audit.ANONYMOUS, audit.AuditAction.LOGIN_FAILED)
    rows = await audit_rows(db_session)

    await db_session.execute(text("SET LOCAL session_replication_role = replica"))
    await db_session.execute(delete(audit_events).where(audit_events.c.id == rows[-2].id))
    await db_session.execute(text("SET LOCAL session_replication_role = origin"))

    check = await verify_chain(db_session)
    assert not check.valid
    assert check.first_invalid_id == rows[-1].id


def test_retention_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings().audit.retention_days == 365

    monkeypatch.setenv("OLLAMAIL_AUDIT_RETENTION_DAYS", "0")

    assert Settings().audit.retention_days == 0
