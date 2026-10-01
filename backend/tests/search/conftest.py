"""Fixtures for search tests: a deterministic fake embedder and synthetic mail data.

All names, addresses and texts are invented (example.* domains, docs/PRIVACY.md).
"""

import math
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.llm import LLMUnavailableError
from app.core.config import SearchSettings
from app.core.ids import uuid7
from app.mail.models import Attachment, Folder, Mailbox, MailboxType, Message
from app.mail.storage import AttachmentStorage
from tests.factories import make_user
from tests.processing.conftest import pipeline  # noqa: F401  (fixture re-export)

DIMENSIONS = SearchSettings().embedding_dimensions

# Concepts of the fake embedding space: words mapping to the same axis are "similar".
TOPICS: dict[str, int] = {
    # 1: travel
    "flight": 1, "flug": 1, "airport": 1, "boarding": 1, "trip": 1, "reise": 1,
    # 2: money
    "invoice": 2, "rechnung": 2, "payment": 2, "zahlung": 2, "bill": 2, "billing": 2,
    # 3: food
    "lunch": 3, "dinner": 3, "pizza": 3, "restaurant": 3, "mittagessen": 3,
    # 4: football
    "football": 4, "soccer": 4, "match": 4, "fussball": 4,
}  # fmt: skip
_WORD = re.compile(r"\w+")


def fake_vector(text: str, *, shift: int = 0) -> list[float]:
    """Bag-of-topics vector; ``shift`` moves the axes (a different "model")."""
    vector = [0.0] * DIMENSIONS
    vector[0] = 0.05  # never a zero vector (cosine distance would be undefined)
    for word in _WORD.findall(text.lower()):
        axis = TOPICS.get(word)
        if axis is not None:
            vector[axis + shift] += 1.0
    norm = math.sqrt(sum(v * v for v in vector))
    return [v / norm for v in vector]


@dataclass
class FakeEmbedder:
    """Models ``fake-a`` (axes 1-4) and ``fake-b`` (axes 11-14)."""

    model: str = "fake-a"
    fail: bool = False
    calls: list[tuple[str, int]] = field(default_factory=list)

    async def current_model(self) -> str:
        return self.model

    async def embed(self, texts: Sequence[str], *, model: str | None = None) -> list[list[float]]:
        model = model or self.model
        if self.fail:
            raise LLMUnavailableError("down")
        self.calls.append((model, len(texts)))
        shift = 10 if model == "fake-b" else 0
        return [fake_vector(text, shift=shift) for text in texts]


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def search_settings() -> SearchSettings:
    return SearchSettings(chunk_size=400, chunk_overlap=60)


@pytest.fixture
def storage(tmp_path: Path) -> AttachmentStorage:
    return AttachmentStorage(tmp_path)


@dataclass
class MailData:
    session: AsyncSession
    storage: AttachmentStorage

    async def user(self) -> uuid.UUID:
        return (await make_user(self.session)).id

    async def mailbox(self, owner_id: uuid.UUID | None) -> uuid.UUID:
        mailbox = Mailbox(
            type=MailboxType.IMAP,
            display_name="Test",
            address="box@example.org",
            owner_user_id=owner_id,
            is_shared=owner_id is None,
        )
        self.session.add(mailbox)
        await self.session.flush()
        return mailbox.id

    async def folder(self, mailbox_id: uuid.UUID, name: str) -> Folder:
        folder = Folder(mailbox_id=mailbox_id, remote_id=name, name=name)
        self.session.add(folder)
        await self.session.flush()
        return folder

    async def message(
        self,
        mailbox_id: uuid.UUID,
        body: str,
        *,
        subject: str = "",
        sender: dict[str, Any] | None = None,
        language: str | None = "en",
        sent_at: datetime | None = None,
        folders: Sequence[Folder] = (),
        attachments: Sequence[tuple[str, str, bytes]] = (),
    ) -> uuid.UUID:
        message = Message(
            id=uuid7(),
            mailbox_id=mailbox_id,
            remote_ref=uuid.uuid4().hex,
            subject=subject,
            sender=sender or {"name": "Erika Example", "address": "erika@example.org"},
            body_text=body,
            body_main=body,
            language=language,
            sent_at=sent_at,
            has_attachments=bool(attachments),
            folders=list(folders),
        )
        self.session.add(message)
        await self.session.flush()
        for filename, content_type, data in attachments:
            attachment_id = uuid7()
            self.session.add(
                Attachment(
                    id=attachment_id,
                    message_id=message.id,
                    mailbox_id=mailbox_id,
                    filename=filename,
                    content_type=content_type,
                    size=len(data),
                    sha256="0" * 64,
                    storage_path=self.storage.write(mailbox_id, attachment_id, data),
                )
            )
        await self.session.flush()
        return message.id


@pytest.fixture
def mail(db_session: AsyncSession, storage: AttachmentStorage) -> MailData:
    return MailData(db_session, storage)
