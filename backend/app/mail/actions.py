"""Mail actions of the user on the server: archive, move, trash (#148).

ollamail is an analysis tool, not a mail client; these are the few changes it makes to a
mailbox besides read/unread and flags (``app.mail.flags``), sent replies (``app.drafts``)
and the opt-in triage write-back (``app.triage.writeback``).

An action runs on the server first (``MailProvider.move``, synchronously in the request),
then the stored message follows: its new ``remote_ref`` (IMAP UIDs change) and folders.
The next sync confirms the folders. Nothing is ever deleted for good: "delete" moves the
message to the trash folder, where the server's own retention applies.

Targets are folders of the mailbox found by role, so every provider needs nothing more
than ``move``:

* ``archive``: the folder with role ``archive``; for label mailboxes (Gmail) the
  ``all`` label, which removes the inbox label.
* ``trash``: the folder with role ``trash``.
* ``move``: any folder or label of the mailbox. Labels (Gmail) are added and the inbox,
  spam and trash labels removed, like moving in Gmail.

The caller checks ``MailboxPermission.ACT``. Each action is recorded in the audit log
(``mail.moved``, IDs only). Privacy: logs carry IDs and codes only.
"""

import enum
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app import audit
from app.core.logging import get_logger
from app.mail.models import Folder, FolderKind, FolderRole, Mailbox, Message
from app.mail.providers.registry import ProviderFactory
from app.mail.sync.engine import credentials_saver, mailbox_config

log = get_logger(__name__)

# Labels a moved message leaves (Gmail): it is no longer in the inbox, spam or trash.
_LEFT_BY_MOVE = frozenset({FolderRole.INBOX, FolderRole.JUNK, FolderRole.TRASH})


class MessageAction(enum.StrEnum):
    ARCHIVE = "archive"
    MOVE = "move"
    TRASH = "trash"


class ActionError(Exception):
    code = "action_failed"


class NoTargetFolderError(ActionError):
    """The mailbox has no archive or trash folder (or it is unknown to us)."""

    def __init__(self, action: MessageAction) -> None:
        super().__init__()
        self.code = f"no_{action.value}_folder"


class UnknownFolderError(ActionError):
    code = "unknown_folder"


class MessageGoneError(ActionError):
    """The message was deleted locally in the meantime (sync, retention)."""

    code = "message_deleted"


@dataclass(frozen=True, slots=True)
class ActionOutcome:
    message: Message
    target: Folder
    # Where the message was before; undo moves it back to ``undo_folder``.
    previous: tuple[Folder, ...]
    undo_folder: Folder | None
    # ``False``: the message already was where the action puts it.
    moved: bool


async def _by_role(session: AsyncSession, mailbox_id: uuid.UUID, role: FolderRole) -> Folder | None:
    return await session.scalar(
        select(Folder)
        .where(Folder.mailbox_id == mailbox_id, Folder.role == role)
        .order_by(Folder.created_at, Folder.id)
        .limit(1)
    )


async def target_folder(
    session: AsyncSession,
    mailbox: Mailbox,
    action: MessageAction,
    folder_id: uuid.UUID | None = None,
) -> Folder:
    """The folder ``action`` moves to. Raises ``NoTargetFolderError`` or
    ``UnknownFolderError``."""
    if action == MessageAction.MOVE:
        folder = await session.scalar(
            select(Folder).where(Folder.id == folder_id, Folder.mailbox_id == mailbox.id)
        )
        if folder is None:
            raise UnknownFolderError
        return folder
    if action == MessageAction.TRASH:
        folder = await _by_role(session, mailbox.id, FolderRole.TRASH)
    else:
        folder = await _by_role(session, mailbox.id, FolderRole.ARCHIVE)
        if folder is None:
            # Gmail has no archive folder: archived mails keep only "All Mail".
            all_mail = await _by_role(session, mailbox.id, FolderRole.ALL)
            if all_mail is not None and all_mail.kind == FolderKind.LABEL:
                folder = all_mail
    if folder is None:
        raise NoTargetFolderError(action)
    return folder


def _folders_after(
    current: Sequence[Folder], target: Folder, action: MessageAction
) -> list[Folder]:
    """The message's folders after the move, as the server will report them."""
    if target.kind != FolderKind.LABEL:
        return [target]
    left = set(_LEFT_BY_MOVE)
    if action == MessageAction.TRASH:
        # Trashed mails are not in "All Mail" either.
        left.add(FolderRole.ALL)
    kept = [f for f in current if f.role not in left and f.id != target.id]
    return [*kept, target]


def _undo_folder(previous: Sequence[Folder], target: Folder) -> Folder | None:
    """Where undo moves the message: the inbox if it was there, else its former folder."""
    candidates = [f for f in previous if f.id != target.id and f.role != FolderRole.ALL]
    inbox = next((f for f in candidates if f.role == FolderRole.INBOX), None)
    return inbox or (candidates[0] if candidates else None)


async def move_message(
    session: AsyncSession,
    message_id: uuid.UUID,
    mailbox: Mailbox,
    action: MessageAction,
    *,
    folder_id: uuid.UUID | None,
    actor: audit.Actor,
    provider_factory: ProviderFactory,
) -> ActionOutcome:
    """Run ``action`` on the server and store the result; the caller commits.

    Raises ``NoTargetFolderError``, ``UnknownFolderError``, ``MessageGoneError`` or the
    provider's ``ProviderError`` (nothing is stored then)."""
    target = await target_folder(session, mailbox, action, folder_id)
    # One action per message at a time; the sync waits for the new reference as well.
    message = await session.scalar(
        select(Message)
        .where(Message.id == message_id, Message.mailbox_id == mailbox.id)
        .options(selectinload(Message.folders))
        .with_for_update(of=Message)
        .execution_options(populate_existing=True)
    )
    if message is None:
        raise MessageGoneError
    previous = tuple(message.folders)
    after = _folders_after(previous, target, action)
    undo = _undo_folder(previous, target)
    if {f.id for f in after} == {f.id for f in previous}:
        return ActionOutcome(message, target, previous, undo, moved=False)

    saver = credentials_saver(
        async_sessionmaker(
            session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint"
        ),
        mailbox.id,
    )
    provider = provider_factory(mailbox_config(mailbox, save_credentials=saver))
    try:
        message.remote_ref = await provider.move(message.remote_ref, target.remote_id)
    finally:
        await provider.aclose()

    message.folders = after
    await audit.record(
        session,
        actor,
        audit.AuditAction.MAIL_MOVED,
        audit.Target.of(audit.TargetType.MAILBOX, mailbox.id),
        {"message_id": message.id, "action": action.value, "folder_id": target.id},
    )
    log.info(
        "mail_message_moved",
        message_id=str(message.id),
        mailbox_id=str(mailbox.id),
        action=action.value,
    )
    return ActionOutcome(message, target, previous, undo, moved=True)
