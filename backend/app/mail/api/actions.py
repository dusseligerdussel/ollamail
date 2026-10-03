"""Mail actions: archive, move, trash (#148), see ``app.mail.actions``.

``POST /messages/{id}/actions`` runs the action on the mail server right away and answers
with the new folders and where undo moves the message back to. Flags and read state are
``PATCH /messages/{id}`` (``app.mail.api.messages``).

Needs ``MailboxPermission.ACT``: owners, and users assigned to a shared mailbox with
``act``. Others get 403 ``read_only``; mails they cannot read answer 404.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.auth.dependencies import CurrentSessionDep
from app.core.db import get_db
from app.core.errors import ProblemError
from app.core.events import Event
from app.mail import access
from app.mail.access import MailboxPermission
from app.mail.actions import (
    MessageAction,
    MessageGoneError,
    NoTargetFolderError,
    UnknownFolderError,
    move_message,
)
from app.mail.api.message_schemas import (
    MessageActionRequest,
    MessageActionResult,
    MessageSummary,
)
from app.mail.api.messages import _message, _summary_fields
from app.mail.api.router import RegistryDep
from app.mail.providers.base import (
    ConfigurationError,
    ConnectionFailedError,
    MessageNotFoundError,
    ProviderError,
)

router = APIRouter(
    prefix="/messages",
    tags=["messages"],
    responses={401: {"description": "Not signed in"}},
)

DbDep = Annotated[AsyncSession, Depends(get_db)]

RESPONSES: dict[int | str, dict[str, Any]] = {
    403: {"description": "Read-only mailbox (error_code read_only)"},
    404: {"description": "No such message"},
    409: {
        "description": "No archive or trash folder (no_archive_folder, no_trash_folder), "
        "the mail is gone on the server (message_not_found) or the mailbox does not allow "
        "changes (error_code)"
    },
    422: {"description": "Unknown or missing folder (unknown_folder)"},
    502: {"description": "The mail server refused the action (error_code)"},
    503: {"description": "The mail server is unreachable"},
}


@router.post("/{message_id}/actions", responses=RESPONSES)
async def run_message_action(
    message_id: uuid.UUID,
    body: MessageActionRequest,
    current: CurrentSessionDep,
    db: DbDep,
    providers: RegistryDep,
) -> MessageActionResult:
    """Archive, move or trash a message on the mail server. Synchronous: when the answer
    arrives, the server has done it. Recorded in the audit log (``mail.moved``)."""
    message = await _message(db, current.user_id, message_id)
    mailbox = await access.get_mailbox(
        db, current.user_id, message.mailbox_id, MailboxPermission.ACT
    )
    if mailbox is None:
        raise ProblemError(403, detail="This mailbox is read-only for you.", error_code="read_only")
    if body.action == MessageAction.MOVE and body.folder_id is None:
        raise ProblemError(422, detail="No folder given.", error_code="unknown_folder")
    try:
        outcome = await move_message(
            db,
            message.id,
            mailbox,
            body.action,
            folder_id=body.folder_id,
            actor=audit.Actor.user(current.user_id),
            provider_factory=providers.create,
        )
    except UnknownFolderError:
        raise ProblemError(422, detail="Unknown folder.", error_code="unknown_folder") from None
    except NoTargetFolderError as exc:
        raise ProblemError(
            409, detail="The mailbox has no folder for this action.", error_code=exc.code
        ) from None
    except MessageGoneError:
        raise ProblemError(404, detail="Message not found.") from None
    except MessageNotFoundError as exc:
        raise ProblemError(
            409, detail="The mail no longer exists on the server.", error_code=exc.code
        ) from None
    except ConfigurationError as exc:
        raise ProblemError(
            409, detail="The mailbox does not allow this action.", error_code=exc.code
        ) from None
    except ConnectionFailedError as exc:
        raise ProblemError(
            503, detail="The mail server is unreachable.", error_code=exc.code
        ) from None
    except ProviderError as exc:
        raise ProblemError(
            502, detail="The mail server did not accept the action.", error_code=exc.code
        ) from None
    if outcome.moved:
        await access.publish_to_readers(
            db,
            mailbox,
            Event(
                type="message.updated",
                ids={"message_id": message.id, "mailbox_id": mailbox.id},
                status=body.action.value,
            ),
        )
    folder_ids = [folder.id for folder in outcome.message.folders]
    summary = MessageSummary(**_summary_fields(outcome.message))
    await db.commit()
    return MessageActionResult(
        message=summary,
        folder_ids=folder_ids,
        undo_folder_id=outcome.undo_folder.id if outcome.undo_folder is not None else None,
    )
