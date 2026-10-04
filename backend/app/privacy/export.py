"""Personal data export (Art. 15/20): one ZIP with JSON files and the digest audio.

Contents (``manifest.json`` lists them):

* ``profile.json``: account, sign-in identities (provider, subject, groups), open notices
  about linked sign-ins (#208), sessions and second factors (passkey names and dates,
  whether TOTP is on; never secrets or codes)
* ``mailboxes.json``: own mailboxes (type, address, settings; never credentials)
* ``triage.json``: own categories, category preferences, sender rules, corrections and
  the triage results of mails in own mailboxes
* ``todos.json``: own todos
* ``todo_export.json``: todo export settings (target, server, user name, list, mode; never
  the password), ``null`` if not connected
* ``digests.json`` and ``digests/<digest_id>.<format>``: digest settings (without the
  feed token), digests with scripts, and their audio files
* ``conversations.json``: "ask your inbox" conversations with answers and citations
* ``reply_drafts.json``: drafting settings (signature, style examples) and own reply drafts
* ``notifications.json``: notification settings (on/off, categories, subject, sound) and
  the devices registered for Web Push (browser, system, push service), ``null`` if never set

Every query is filtered by the exporting user (``user_id`` or the owner of the mailbox),
so an export never contains data of other users. Mails themselves are not part of the
export: they stay in the user's mail account, where the provider offers its own export.
"""

import asyncio
import enum
import json
import os
import tempfile
import uuid
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth.mfa.service import factors as mfa_factors
from app.auth.models import AuthSession, Identity, IdentityLinkNotice
from app.digest.models import Digest, DigestUserSettings
from app.digest.storage import DigestStorage
from app.drafts.models import DraftSettings, ReplyDraft
from app.mail.models import Mailbox, Message
from app.notifications.models import NotificationSettings, PushSubscription
from app.notifications.webpush import push_service
from app.rag.models import RagConversation, RagMessage
from app.todos.export.models import TodoExportTarget
from app.todos.models import Todo
from app.triage.models import (
    TriageCategory,
    TriageCategoryPreference,
    TriageFeedback,
    TriageResult,
    TriageSenderRule,
)
from app.users.models import User

FORMAT_VERSION = 1


class UserNotFoundError(Exception):
    """The user was deleted before the export ran."""


def _json_default(value: object) -> object:
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, enum.Enum):
        return value.value
    raise TypeError(type(value).__name__)


def _dump(data: object) -> bytes:
    return json.dumps(data, default=_json_default, ensure_ascii=False, indent=2).encode()


def _row(obj: object, fields: Iterable[str]) -> dict[str, Any]:
    return {name: getattr(obj, name) for name in fields}


@dataclass
class ExportContent:
    """What goes into the ZIP: JSON documents and files to copy (archive name → path)."""

    documents: dict[str, object] = field(default_factory=dict)
    files: dict[str, Path] = field(default_factory=dict)


async def _profile(session: AsyncSession, user: User) -> dict[str, Any]:
    identities = await session.scalars(
        select(Identity).where(Identity.user_id == user.id).order_by(Identity.created_at)
    )
    link_notices = await session.scalars(
        select(IdentityLinkNotice)
        .where(IdentityLinkNotice.user_id == user.id)
        .order_by(IdentityLinkNotice.created_at)
    )
    sessions = await session.scalars(
        select(AuthSession).where(AuthSession.user_id == user.id).order_by(AuthSession.created_at)
    )
    return {
        "user": _row(
            user,
            (
                "id",
                "email",
                "display_name",
                "role",
                "language",
                "timezone",
                "is_active",
                "created_at",
                "last_login_at",
            ),
        ),
        "identities": [
            _row(identity, ("provider", "subject", "groups", "created_at", "last_used_at"))
            for identity in identities
        ],
        "identity_link_notices": [
            _row(notice, ("provider", "created_at")) for notice in link_notices
        ],
        "sessions": [
            _row(item, ("provider", "user_agent", "created_at", "last_seen_at", "expires_at"))
            for item in sessions
        ],
        "second_factors": await _second_factors(session, user.id),
    }


async def _second_factors(session: AsyncSession, user_id: uuid.UUID) -> dict[str, Any]:
    found = await mfa_factors(session, user_id)
    return {
        "totp": found.totp,
        "passkeys": [
            _row(passkey, ("name", "backed_up", "created_at", "last_used_at"))
            for passkey in found.passkeys
        ],
        "recovery_codes_remaining": found.recovery_remaining,
    }


async def _mailboxes(session: AsyncSession, user_id: uuid.UUID) -> list[Mailbox]:
    return list(
        await session.scalars(
            select(Mailbox).where(Mailbox.owner_user_id == user_id).order_by(Mailbox.created_at)
        )
    )


def _mailbox_rows(mailboxes: list[Mailbox]) -> list[dict[str, Any]]:
    return [
        _row(
            mailbox,
            (
                "id",
                "type",
                "display_name",
                "address",
                "provider_settings",
                "sync_enabled",
                "sync_settings",
                "created_at",
            ),
        )
        for mailbox in mailboxes
    ]


async def _triage(
    session: AsyncSession, user_id: uuid.UUID, mailbox_ids: list[uuid.UUID]
) -> dict[str, Any]:
    categories = await session.scalars(
        select(TriageCategory)
        .where(or_(TriageCategory.owner_user_id == user_id, TriageCategory.owner_user_id.is_(None)))
        .order_by(TriageCategory.position, TriageCategory.created_at)
    )
    preferences = await session.scalars(
        select(TriageCategoryPreference).where(TriageCategoryPreference.user_id == user_id)
    )
    rules = await session.scalars(
        select(TriageSenderRule)
        .where(TriageSenderRule.user_id == user_id)
        .order_by(TriageSenderRule.created_at)
    )
    corrections = await session.scalars(
        select(TriageFeedback)
        .where(TriageFeedback.user_id == user_id)
        .order_by(TriageFeedback.created_at)
    )
    results = await session.scalars(
        select(TriageResult)
        .join(Message, Message.id == TriageResult.message_id)
        .where(Message.mailbox_id.in_(mailbox_ids))
        .order_by(TriageResult.created_at)
    )
    return {
        "categories": [
            {
                **_row(category, ("id", "builtin_key", "name", "description", "position")),
                "own": category.owner_user_id == user_id,
            }
            for category in categories
        ],
        "category_preferences": [
            _row(preference, ("category_id", "hidden", "position")) for preference in preferences
        ],
        "sender_rules": [
            _row(rule, ("id", "sender", "category_id", "priority", "created_at")) for rule in rules
        ],
        "corrections": [
            _row(item, ("message_id", "category_id", "priority", "created_at"))
            for item in corrections
        ],
        "results": [
            _row(
                result,
                ("message_id", "category_id", "priority", "reason", "rule", "source", "model"),
            )
            for result in results
        ],
    }


async def _todos(session: AsyncSession, user_id: uuid.UUID) -> list[dict[str, Any]]:
    todos = await session.scalars(
        select(Todo).where(Todo.user_id == user_id).order_by(Todo.created_at)
    )
    return [
        _row(
            todo,
            (
                "id",
                "title",
                "description",
                "due_date",
                "priority",
                "status",
                "completed_at",
                "is_manual",
                "mailbox_id",
                "message_id",
                "created_at",
                "updated_at",
            ),
        )
        for todo in todos
    ]


async def _todo_export(session: AsyncSession, user_id: uuid.UUID) -> dict[str, Any] | None:
    target = await session.scalar(
        select(TodoExportTarget).where(TodoExportTarget.user_id == user_id)
    )
    if target is None:
        return None
    row = _row(
        target,
        ("sink", "list_name", "mode", "last_sync_at", "last_error", "created_at", "updated_at"),
    )
    # Server and account, never the password.
    row["url"] = target.config.get("url")
    row["username"] = target.config.get("username")
    return row


async def _digests(
    session: AsyncSession, user_id: uuid.UUID, storage: DigestStorage, content: ExportContent
) -> dict[str, Any]:
    settings = await session.scalar(
        select(DigestUserSettings).where(DigestUserSettings.user_id == user_id)
    )
    digests = await session.scalars(
        select(Digest).where(Digest.user_id == user_id).order_by(Digest.created_at)
    )
    rows = []
    for digest in digests:
        audio = []
        for fmt, info in sorted((digest.audio or {}).items()):
            try:
                path = storage.resolve(str(info.get("path", "")))
            except ValueError:
                continue
            # Only files in the user's own digest directory.
            if path.parent.name != str(user_id) or not path.is_file():
                continue
            name = f"digests/{digest.id}.{fmt}"
            content.files[name] = path
            audio.append({"format": fmt, "file": name})
        rows.append(
            {
                **_row(
                    digest,
                    (
                        "id",
                        "trigger",
                        "status",
                        "period_start",
                        "period_end",
                        "language",
                        "voice",
                        "length",
                        "mailbox_ids",
                        "title",
                        "script",
                        "references",
                        "message_count",
                        "todo_count",
                        "model",
                        "duration_seconds",
                        "generated_at",
                        "created_at",
                    ),
                ),
                "audio": audio,
            }
        )
    return {
        "settings": None
        if settings is None
        else {
            **_row(
                settings,
                (
                    "enabled",
                    "delivery_time",
                    "timezone",
                    "weekdays",
                    "language",
                    "voice",
                    "length",
                    "mailbox_ids",
                ),
            ),
            "feed_enabled": settings.feed_token_hash is not None,
        },
        "digests": rows,
    }


async def _conversations(session: AsyncSession, user_id: uuid.UUID) -> list[dict[str, Any]]:
    conversations = await session.scalars(
        select(RagConversation)
        .where(RagConversation.user_id == user_id)
        .options(selectinload(RagConversation.messages).selectinload(RagMessage.citations))
        .order_by(RagConversation.created_at)
    )
    return [
        {
            **_row(conversation, ("id", "title", "created_at", "updated_at")),
            "messages": [
                {
                    **_row(
                        message,
                        ("position", "role", "content", "filters", "status", "model", "created_at"),
                    ),
                    "citations": [
                        _row(
                            citation,
                            ("number", "message_id", "mailbox_id", "source", "heading", "snippet"),
                        )
                        for citation in message.citations
                    ],
                }
                for message in sorted(conversation.messages, key=lambda item: item.position)
            ],
        }
        for conversation in conversations.unique()
    ]


async def _reply_drafts(session: AsyncSession, user_id: uuid.UUID) -> dict[str, Any]:
    settings = await session.scalar(select(DraftSettings).where(DraftSettings.user_id == user_id))
    drafts = await session.scalars(
        select(ReplyDraft).where(ReplyDraft.user_id == user_id).order_by(ReplyDraft.created_at)
    )
    return {
        "settings": _row(settings, ("signature", "style_examples")) if settings else None,
        "drafts": [
            _row(
                draft,
                (
                    "id",
                    "status",
                    "mailbox_id",
                    "message_id",
                    "reply_all",
                    "to",
                    "cc",
                    "subject",
                    "body",
                    "quote_original",
                    "instruction",
                    "language",
                    "model",
                    "sent_at",
                    "created_at",
                    "updated_at",
                ),
            )
            for draft in drafts
        ],
    }


async def _notifications(session: AsyncSession, user_id: uuid.UUID) -> dict[str, Any] | None:
    settings = await session.scalar(
        select(NotificationSettings).where(NotificationSettings.user_id == user_id)
    )
    devices = list(
        await session.scalars(
            select(PushSubscription)
            .where(PushSubscription.user_id == user_id)
            .order_by(PushSubscription.created_at, PushSubscription.id)
        )
    )
    if settings is None and not devices:
        return None
    stored = (
        {
            **_row(settings, ("enabled", "show_subject", "sound", "updated_at")),
            "category_ids": [str(category_id) for category_id in settings.category_ids],
        }
        if settings is not None
        else {}
    )
    return {
        **stored,
        # Devices for Web Push: labels and the push service, not the endpoint and keys
        # (they only let a server send to the device).
        "push_devices": [
            {
                **_row(device, ("id", "browser", "os", "mobile", "created_at", "last_sent_at")),
                "push_service": push_service(device.subscription["endpoint"]),
            }
            for device in devices
        ],
    }


async def collect(
    session: AsyncSession, user_id: uuid.UUID, digest_storage: DigestStorage
) -> ExportContent:
    """Everything stored about ``user_id``, as JSON-ready data."""
    user = await session.get(User, user_id)
    if user is None:
        raise UserNotFoundError
    content = ExportContent()
    mailboxes = await _mailboxes(session, user_id)
    content.documents["profile.json"] = await _profile(session, user)
    content.documents["mailboxes.json"] = _mailbox_rows(mailboxes)
    content.documents["triage.json"] = await _triage(
        session, user_id, [mailbox.id for mailbox in mailboxes]
    )
    content.documents["todos.json"] = await _todos(session, user_id)
    content.documents["todo_export.json"] = await _todo_export(session, user_id)
    content.documents["digests.json"] = await _digests(session, user_id, digest_storage, content)
    content.documents["conversations.json"] = await _conversations(session, user_id)
    content.documents["reply_drafts.json"] = await _reply_drafts(session, user_id)
    content.documents["notifications.json"] = await _notifications(session, user_id)
    return content


def write_zip(
    content: ExportContent, target: Path, *, export_id: uuid.UUID, created_at: datetime
) -> int:
    """Write the ZIP atomically (temporary file, then rename); returns its size."""
    target.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "format_version": FORMAT_VERSION,
        "export_id": export_id,
        "created_at": created_at,
        "files": sorted([*content.documents, *content.files]),
    }
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f"{target.name}.tmp-")
    try:
        with (
            os.fdopen(fd, "wb") as handle,
            zipfile.ZipFile(handle, "w", compression=zipfile.ZIP_DEFLATED) as archive,
        ):
            archive.writestr("manifest.json", _dump(manifest))
            for name, data in content.documents.items():
                archive.writestr(name, _dump(data))
            for name, path in content.files.items():
                # Audio is already compressed.
                archive.write(path, name, compress_type=zipfile.ZIP_STORED)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return target.stat().st_size


async def build(
    session: AsyncSession,
    user_id: uuid.UUID,
    target: Path,
    *,
    export_id: uuid.UUID,
    created_at: datetime,
    digest_storage: DigestStorage,
) -> int:
    content = await collect(session, user_id, digest_storage)
    return await asyncio.to_thread(
        write_zip, content, target, export_id=export_id, created_at=created_at
    )
