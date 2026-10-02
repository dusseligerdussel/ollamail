"""Fixtures for privacy tests: the mailbox API app (fake mail server, data directory in
``tmp_path``) and a helper that gives a user data in every module. All names, addresses
and texts are invented."""

import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from httpx import AsyncClient
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.mfa.models import Passkey, PendingLogin, RecoveryCode, TotpFactor
from app.auth.models import Invitation
from app.core.config import get_settings
from app.digest.models import Digest, DigestLength, DigestStatus, DigestTrigger, DigestUserSettings
from app.digest.storage import DigestStorage
from app.drafts.models import DraftSettings, ReplyDraft
from app.mail.models import Attachment, Mailbox, MailboxAssignment, MailboxType, Message
from app.mail.storage import AttachmentStorage
from app.processing.models import MailboxProcessingSettings, MessageProcessing, StepStatus
from app.rag.models import RagCitation, RagConversation, RagMessage, RagRole
from app.scim.models import ScimGroup, ScimUser, scim_group_members
from app.search.models import ChunkSource, SearchChunk, SearchEmbedding
from app.todos.export.models import TodoExportTarget
from app.todos.models import Todo
from app.triage.models import (
    TriageCategory,
    TriageCategoryPreference,
    TriageFeedback,
    TriageMailboxSettings,
    TriageResult,
    TriageSenderRule,
    TriageSource,
)
from tests.auth.conftest import _cheap_hashing  # noqa: F401
from tests.mail.api.conftest import (  # noqa: F401
    NOW,
    FakeServer,
    add_mailbox,
    anonymous,
    app,
    bob,
    data_dir,
    erika,
    run_sync,
    server,
    storage,
    sync_requests,
)


@dataclass
class SeededData:
    """IDs and files of one user's data in every module."""

    user_id: uuid.UUID
    mailbox_id: uuid.UUID
    marker: str
    todo_id: uuid.UUID
    category_id: uuid.UUID
    conversation_id: uuid.UUID
    digest_id: uuid.UUID
    attachment_paths: list[str] = field(default_factory=list)
    audio_paths: list[Path] = field(default_factory=list)


async def current_user_id(client: AsyncClient) -> uuid.UUID:
    response = await client.get("/auth/me")
    assert response.status_code == 200
    return uuid.UUID(response.json()["id"])


async def seed_user_data(
    session: AsyncSession,
    client: AsyncClient,
    mail_server: FakeServer,
    attachments: AttachmentStorage,
    root: Path,
    *,
    address: str,
    marker: str,
) -> SeededData:
    """A mailbox with synced mails and attachments, plus rows in every module that keeps
    user data. ``marker`` appears in the user's own texts (todos, categories, ...)."""
    user_id = await current_user_id(client)
    mailbox_id = uuid.UUID(str((await add_mailbox(client, address=address))["id"]))
    await run_sync(session, mailbox_id, mail_server, attachments)
    message = await session.scalar(
        select(Message).where(Message.mailbox_id == mailbox_id, Message.has_attachments)
    )
    assert message is not None
    attachment = await session.scalar(select(Attachment).where(Attachment.message_id == message.id))
    assert attachment is not None

    # Access to a shared mailbox (#34): the assignment goes with the user, the shared
    # mailbox stays (it is created once, by the first seeded user).
    shared = await session.scalar(select(Mailbox).where(Mailbox.is_shared))
    if shared is None:
        shared = Mailbox(
            type=MailboxType.IMAP,
            display_name="Team",
            address="team@example.org",
            is_shared=True,
        )
        session.add(shared)
        await session.flush()
    session.add(MailboxAssignment(mailbox_id=shared.id, user_id=user_id))
    # Second factors (#96) and a pending login.
    session.add_all(
        [
            TotpFactor(user_id=user_id, secret="JBSWY3DPEHPK3PXP"),
            Passkey(
                user_id=user_id,
                credential_id=f"credential-{marker}".encode(),
                public_key=b"public-key",
                sign_count=0,
                name=f"Passkey {marker}",
            ),
            RecoveryCode(user_id=user_id, code_hash=f"code-{marker}".encode().ljust(32, b"-")),
            PendingLogin(
                token_hash=f"pending-{marker}".encode().ljust(32, b"-"),
                purpose="verify",
                user_id=user_id,
                expires_at=NOW + timedelta(minutes=5),
            ),
        ]
    )

    category = TriageCategory(owner_user_id=user_id, name=f"Category {marker}", description="")
    session.add(category)
    await session.flush()
    todo = Todo(
        user_id=user_id,
        mailbox_id=mailbox_id,
        message_id=message.id,
        thread_id=message.thread_id,
        title=f"Todo {marker}",
    )
    chunk = SearchChunk(
        message_id=message.id,
        mailbox_id=mailbox_id,
        attachment_id=attachment.id,
        source=ChunkSource.ATTACHMENT,
        ordinal=0,
        heading="Attachment",
        content=f"Chunk {marker}",
        ts_config="simple",
    )
    conversation = RagConversation(user_id=user_id, title=f"Conversation {marker}")
    export_target = TodoExportTarget(
        user_id=user_id,
        sink="caldav",
        config={"url": "https://dav.example.org/", "username": marker, "password": "x"},
        list_id=f"/calendars/{marker}/tasks/",
        list_name=f"Tasks {marker}",
    )
    session.add_all([todo, chunk, conversation, export_target])
    await session.flush()
    question = RagMessage(
        conversation_id=conversation.id, position=0, role=RagRole.USER, content=f"Ask {marker}"
    )
    answer = RagMessage(
        conversation_id=conversation.id,
        position=1,
        role=RagRole.ASSISTANT,
        content=f"Answer {marker} [1]",
    )
    session.add_all([question, answer])
    await session.flush()

    digest = Digest(
        user_id=user_id,
        trigger=DigestTrigger.MANUAL,
        status=DigestStatus.READY,
        period_start=NOW - timedelta(days=1),
        period_end=NOW,
        language="en",
        length=DigestLength.SHORT,
        mailbox_ids=[mailbox_id],
        title=f"Digest {marker}",
        script=f"Script {marker}",
    )
    session.add(digest)
    await session.flush()
    digests = DigestStorage(root)
    target = digests.target(user_id, digest.id)
    target.parent.mkdir(parents=True, exist_ok=True)
    audio = target.with_suffix(".mp3")
    audio.write_bytes(f"ID3 audio {marker}".encode())
    digest.audio = {"mp3": {"path": digests.relative(audio), "size_bytes": audio.stat().st_size}}

    session.add_all(
        [
            SearchEmbedding(
                chunk_id=chunk.id,
                model="test-embed",
                embedding=[0.1] * get_settings().search.embedding_dimensions,
            ),
            RagCitation(
                answer_id=answer.id,
                number=1,
                message_id=message.id,
                mailbox_id=mailbox_id,
                attachment_id=attachment.id,
                source=ChunkSource.ATTACHMENT,
                heading="Attachment",
                snippet=f"Snippet {marker}",
            ),
            MessageProcessing(
                message_id=message.id, step="todos", version=1, status=StepStatus.DONE
            ),
            MailboxProcessingSettings(mailbox_id=mailbox_id, enabled=True),
            TriageMailboxSettings(mailbox_id=mailbox_id),
            TriageResult(
                message_id=message.id,
                category_id=category.id,
                priority=2,
                source=TriageSource.LLM,
                reason=f"Reason {marker}",
            ),
            TriageFeedback(
                user_id=user_id, message_id=message.id, category_id=category.id, priority=1
            ),
            TriageSenderRule(
                user_id=user_id, sender=f"sender-{marker}.example.org", category_id=category.id
            ),
            TriageCategoryPreference(user_id=user_id, category_id=category.id, hidden=False),
            DigestUserSettings(user_id=user_id, enabled=True),
            ReplyDraft(
                user_id=user_id,
                mailbox_id=mailbox_id,
                message_id=message.id,
                thread_id=message.thread_id,
                subject=f"Re: {marker}",
                body=f"Draft {marker}",
            ),
            DraftSettings(user_id=user_id, signature=f"Signature {marker}"),
            Invitation(
                user_id=user_id, token_hash=os.urandom(32), expires_at=NOW + timedelta(days=1)
            ),
            ScimUser(user_id=user_id, user_name=f"scim-{marker}", external_id=marker),
        ]
    )
    # Like the shared mailbox, the SCIM group stays when a member is deleted.
    group = await session.scalar(select(ScimGroup))
    if group is None:
        group = ScimGroup(display_name="Team")
        session.add(group)
        await session.flush()
    await session.execute(insert(scim_group_members).values(group_id=group.id, user_id=user_id))
    await session.commit()
    paths = list(
        await session.scalars(
            select(Attachment.storage_path).where(Attachment.mailbox_id == mailbox_id)
        )
    )
    return SeededData(
        user_id=user_id,
        mailbox_id=mailbox_id,
        marker=marker,
        todo_id=todo.id,
        category_id=category.id,
        conversation_id=conversation.id,
        digest_id=digest.id,
        attachment_paths=paths,
        audio_paths=[audio],
    )


def utcnow() -> datetime:
    return datetime.now(UTC)
