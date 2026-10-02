"""Audit log for admins: filterable list, CSV export, hash chain check.

Only admins have access (``AdminSessionDep``). Rows contain IDs, reason codes and counts,
never mail content (docs/PRIVACY.md).
"""

import csv
import io
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.events import Actor, AuditAction, TargetType
from app.audit.schemas import AuditChainStatus, AuditEventPage, AuditEventRead
from app.audit.service import AuditFilter, iter_events, list_events, record, verify_chain
from app.auth.dependencies import AdminSessionDep
from app.core.db import get_db

router = APIRouter(
    prefix="/audit",
    tags=["audit"],
    responses={401: {"description": "Not signed in"}, 403: {"description": "Not an admin"}},
)

DbDep = Annotated[AsyncSession, Depends(get_db)]

CSV_COLUMNS = (
    "id",
    "occurred_at",
    "action",
    "actor_kind",
    "actor_id",
    "actor_name",
    "target_type",
    "target_id",
    "target_name",
    "details",
)


def audit_filter(
    action: AuditAction | None = None,
    actor_id: uuid.UUID | None = None,
    target_type: TargetType | None = None,
    target_id: Annotated[str | None, Query(max_length=64)] = None,
    since: Annotated[datetime | None, Query(description="Inclusive lower bound")] = None,
    until: Annotated[datetime | None, Query(description="Exclusive upper bound")] = None,
) -> AuditFilter:
    return AuditFilter(
        action=action,
        actor_id=actor_id,
        target_type=target_type,
        target_id=target_id,
        since=since,
        until=until,
    )


FilterDep = Annotated[AuditFilter, Depends(audit_filter)]


def _read(row: Any) -> AuditEventRead:
    return AuditEventRead.model_validate(row._mapping, from_attributes=False)


@router.get("/events")
async def get_events(
    _: AdminSessionDep,
    db: DbDep,
    filters: FilterDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before: Annotated[int | None, Query(ge=1, description="Cursor: `next_before`")] = None,
) -> AuditEventPage:
    """Audit events, newest first."""
    rows = await list_events(db, filters, limit=limit + 1, before=before)
    items = [_read(row) for row in rows[:limit]]
    next_before = items[-1].id if len(rows) > limit else None
    return AuditEventPage(items=items, next_before=next_before)


def _csv_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, dict):
        value = json.dumps(value, sort_keys=True, separators=(",", ":"))
    cell = str(value)
    # Spreadsheet formula injection (display names are user input).
    if cell.startswith(("=", "+", "-", "@", "\t", "\r")):
        cell = "'" + cell
    return cell


async def _csv_lines(db: AsyncSession, filters: AuditFilter) -> AsyncIterator[str]:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    async for row in iter_events(db, filters):
        event = _read(row).model_dump()
        writer.writerow([_csv_cell(event[column]) for column in CSV_COLUMNS])
        if buffer.tell() > 64 * 1024:
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate()
    yield buffer.getvalue()


@router.get(
    "/events/export",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/csv": {}}, "description": "CSV, newest first"}},
)
async def export_events(admin: AdminSessionDep, db: DbDep, filters: FilterDep) -> StreamingResponse:
    """All matching audit events as CSV. The export itself is recorded."""
    await record(db, Actor.user(admin.user_id), AuditAction.AUDIT_EXPORTED)
    await db.commit()
    filename = f"audit-log-{datetime.now(UTC):%Y%m%d-%H%M%S}.csv"
    return StreamingResponse(
        _csv_lines(db, filters),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/verify")
async def verify(_: AdminSessionDep, db: DbDep) -> AuditChainStatus:
    """Recompute the hash chain to detect changed or removed entries."""
    result = await verify_chain(db)
    return AuditChainStatus(
        valid=result.valid, checked=result.checked, first_invalid_id=result.first_invalid_id
    )
