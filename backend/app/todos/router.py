"""Todo API. Users only ever see and change their own todos; todos are linked only to
mails in their own mailboxes."""

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentSessionDep
from app.core.db import get_db
from app.core.errors import ProblemError
from app.todos import service
from app.todos.models import Todo, TodoStatus
from app.todos.schemas import TodoCreate, TodoRead, TodoUpdate

router = APIRouter(
    prefix="/todos",
    tags=["todos"],
    responses={401: {"description": "Not signed in"}},
)

DbDep = Annotated[AsyncSession, Depends(get_db)]
NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"description": "No such todo"}}


async def _todo(db: AsyncSession, user_id: uuid.UUID, todo_id: uuid.UUID) -> Todo:
    todo = await service.get_todo(db, user_id, todo_id)
    if todo is None:
        raise ProblemError(404, detail="Todo not found.")
    return todo


@router.get("")
async def list_todos(
    current: CurrentSessionDep,
    db: DbDep,
    status_: Annotated[
        list[TodoStatus] | None, Query(alias="status", description="Repeat for several.")
    ] = None,
    mailbox_id: uuid.UUID | None = None,
    message_id: Annotated[
        uuid.UUID | None, Query(description="Todos from (or linked to) this mail.")
    ] = None,
    due_before: Annotated[date | None, Query(description="Due on or before this day.")] = None,
    due_after: Annotated[date | None, Query(description="Due on or after this day.")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[TodoRead]:
    """Own todos, earliest due date first (todos without due date last)."""
    filters = service.TodoFilter(
        status=status_ or (),
        mailbox_id=mailbox_id,
        message_id=message_id,
        due_before=due_before,
        due_after=due_after,
    )
    todos = await service.list_todos(db, current.user_id, filters, limit=limit, offset=offset)
    return [TodoRead.model_validate(todo) for todo in todos]


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    responses={404: {"description": "Linked message not found"}},
)
async def create_todo(body: TodoCreate, current: CurrentSessionDep, db: DbDep) -> TodoRead:
    """Create a todo by hand, optionally linked to one of the user's mails."""
    source = None
    if body.message_id is not None:
        source = await service.own_message(db, current.user_id, body.message_id)
        if source is None:
            raise ProblemError(404, detail="Message not found.")
    todo = service.create_todo(db, current.user_id, body, source)
    await db.commit()
    await db.refresh(todo)
    return TodoRead.model_validate(todo)


@router.get("/{todo_id}", responses=NOT_FOUND)
async def get_todo(todo_id: uuid.UUID, current: CurrentSessionDep, db: DbDep) -> TodoRead:
    return TodoRead.model_validate(await _todo(db, current.user_id, todo_id))


@router.patch("/{todo_id}", responses=NOT_FOUND)
async def update_todo(
    todo_id: uuid.UUID, body: TodoUpdate, current: CurrentSessionDep, db: DbDep
) -> TodoRead:
    """Edit a todo or change its status (open, done, dismissed)."""
    todo = await _todo(db, current.user_id, todo_id)
    service.update_todo(todo, body)
    await db.commit()
    await db.refresh(todo)
    return TodoRead.model_validate(todo)


@router.delete("/{todo_id}", status_code=status.HTTP_204_NO_CONTENT, responses=NOT_FOUND)
async def delete_todo(todo_id: uuid.UUID, current: CurrentSessionDep, db: DbDep) -> Response:
    todo = await _todo(db, current.user_id, todo_id)
    await db.delete(todo)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
