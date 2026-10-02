"""Index and search against PostgreSQL with fake embeddings. All mail data is synthetic."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import SearchSettings
from app.mail.models import Message
from app.mail.storage import AttachmentStorage
from app.search import service
from app.search.models import SearchChunk, SearchEmbedding
from app.search.service import SearchFilters, fuse, index_message, search
from tests.search.conftest import FakeEmbedder, MailData
from tests.search.documents import make_pdf

pytestmark = pytest.mark.db


async def _index(
    mail: MailData, embedder: FakeEmbedder, settings: SearchSettings, *message_ids: uuid.UUID
) -> None:
    for message_id in message_ids:
        await index_message(
            mail.session, message_id, embedder=embedder, storage=mail.storage, settings=settings
        )


async def _chunks(session: AsyncSession, message_id: uuid.UUID) -> list[SearchChunk]:
    return list(
        await session.scalars(
            select(SearchChunk)
            .where(SearchChunk.message_id == message_id)
            .order_by(SearchChunk.ordinal)
        )
    )


async def _embedding_models(session: AsyncSession) -> dict[str, int]:
    rows = await session.execute(
        select(SearchEmbedding.model, func.count()).group_by(SearchEmbedding.model)
    )
    return {model: int(count) for model, count in rows.all()}


# --- fusion --------------------------------------------------------------------------


def test_fuse_rewards_items_found_by_both_lists() -> None:
    a, b, c, d = (uuid.uuid4() for _ in range(4))

    fused = fuse([[a, b, c], [d, c]], k=60)

    assert [item for item, _ in fused] == [c, a, d, b]
    assert fused[0][1] == pytest.approx(1 / 63 + 1 / 62)
    assert fuse([[], []], k=60) == []


# --- indexing ------------------------------------------------------------------------


async def test_message_is_chunked_with_heading_and_embedded(
    mail: MailData, embedder: FakeEmbedder, search_settings: SearchSettings
) -> None:
    mailbox = await mail.mailbox(await mail.user())
    body = "\n\n".join(f"Paragraph {i} about the project plan and next steps." for i in range(30))
    message_id = await mail.message(
        mailbox,
        body,
        subject="Project plan",
        sent_at=datetime(2026, 9, 1, 9, 30, tzinfo=UTC),
    )

    result = await index_message(
        mail.session, message_id, embedder=embedder, storage=mail.storage, settings=search_settings
    )

    chunks = await _chunks(mail.session, message_id)
    assert result.chunks == len(chunks) > 1
    assert result.embedded is True
    assert all(c.mailbox_id == mailbox and c.source == "body" for c in chunks)
    assert chunks[0].heading == (
        "From: Erika Example <erika@example.org>\nDate: 2026-09-01\nSubject: Project plan"
    )
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    assert all(len(c.content) <= search_settings.chunk_size for c in chunks)
    assert await _embedding_models(mail.session) == {"fake-a": len(chunks)}
    assert await service.active_model(mail.session) == "fake-a"


async def test_quotes_and_signatures_are_not_indexed(
    mail: MailData, embedder: FakeEmbedder, search_settings: SearchSettings
) -> None:
    mailbox = await mail.mailbox(await mail.user())
    message_id = await mail.message(mailbox, "Sounds good.")
    message = await mail.session.get(Message, message_id)
    assert message is not None
    message.body_text = "Sounds good.\n\n> Old quoted text about pineapples\n-- \nSignature"
    await mail.session.flush()

    await _index(mail, embedder, search_settings, message_id)

    assert [c.content for c in await _chunks(mail.session, message_id)] == ["Sounds good."]


async def test_message_without_text_is_still_found_by_subject(
    mail: MailData, embedder: FakeEmbedder, search_settings: SearchSettings
) -> None:
    owner = await mail.user()
    message_id = await mail.message(await mail.mailbox(owner), "", subject="Boarding pass")

    await _index(mail, embedder, search_settings, message_id)

    chunks = await _chunks(mail.session, message_id)
    assert [(c.content, c.heading.endswith("Subject: Boarding pass")) for c in chunks] == [
        ("", True)
    ]
    hits = await search(mail.session, owner, "boarding", embedder=None, settings=search_settings)
    assert [hit.message_id for hit in hits] == [message_id]


async def test_reindexing_replaces_chunks(
    mail: MailData, embedder: FakeEmbedder, search_settings: SearchSettings
) -> None:
    mailbox = await mail.mailbox(await mail.user())
    message_id = await mail.message(mailbox, "First version of the text.")
    await _index(mail, embedder, search_settings, message_id)
    message = await mail.session.get(Message, message_id)
    assert message is not None
    message.body_main = "Second version of the text."
    await mail.session.flush()

    await _index(mail, embedder, search_settings, message_id)
    await _index(mail, embedder, search_settings, message_id)

    chunks = await _chunks(mail.session, message_id)
    assert [c.content for c in chunks] == ["Second version of the text."]
    assert await _embedding_models(mail.session) == {"fake-a": 1}


async def test_attachments_are_extracted_and_chunked(
    mail: MailData, embedder: FakeEmbedder, search_settings: SearchSettings
) -> None:
    owner = await mail.user()
    message_id = await mail.message(
        await mail.mailbox(owner),
        "See attached.",
        subject="Documents",
        attachments=[
            (
                "invoice.pdf",
                "application/pdf",
                make_pdf("Die Rechnung Nr. 4711 vom September ist bis zum Monatsende zu bezahlen."),
            ),
            ("notes.txt", "text/plain", b"Lunch with the team on Friday"),
            ("photo.jpg", "image/jpeg", b"\xff\xd8\xff"),
            ("huge.txt", "text/plain", b"x" * 3000),
        ],
    )
    settings = search_settings.model_copy(update={"attachment_max_bytes": 2048})

    result = await index_message(
        mail.session, message_id, embedder=embedder, storage=mail.storage, settings=settings
    )

    assert dict(result.extraction) == {"ok": 2, "unsupported": 1, "too_large": 1}
    chunks = await _chunks(mail.session, message_id)
    assert [(c.source, c.content) for c in chunks[:1]] == [("body", "See attached.")]
    by_content = {c.content: c for c in chunks[1:]}
    assert set(by_content) == {
        "Die Rechnung Nr. 4711 vom September ist bis zum Monatsende zu bezahlen.",
        "Lunch with the team on Friday",
    }
    invoice = by_content["Die Rechnung Nr. 4711 vom September ist bis zum Monatsende zu bezahlen."]
    assert invoice.source == "attachment"
    assert invoice.heading.endswith("Subject: Documents\nAttachment: invoice.pdf")
    assert invoice.attachment_id is not None
    # The attachment's language decides the text search configuration.
    assert str(invoice.ts_config) == "german"
    hits = await search(mail.session, owner, "Rechnungen", embedder=None, settings=settings)
    assert [hit.chunk_id for hit in hits] == [invoice.id]


async def test_missing_attachment_file_is_skipped(
    mail: MailData, embedder: FakeEmbedder, search_settings: SearchSettings
) -> None:
    mailbox = await mail.mailbox(await mail.user())
    message_id = await mail.message(
        mailbox, "Body", attachments=[("a.txt", "text/plain", b"attachment text")]
    )
    mail.storage.delete_mailbox(mailbox)

    result = await index_message(
        mail.session, message_id, embedder=embedder, storage=mail.storage, settings=search_settings
    )

    assert dict(result.extraction) == {"missing": 1}
    assert result.chunks == 1


async def test_chunk_limit_per_message(
    mail: MailData, embedder: FakeEmbedder, search_settings: SearchSettings
) -> None:
    mailbox = await mail.mailbox(await mail.user())
    message_id = await mail.message(mailbox, "word " * 5000)
    settings = search_settings.model_copy(update={"max_chunks_per_message": 3})

    result = await index_message(
        mail.session, message_id, embedder=embedder, storage=mail.storage, settings=settings
    )

    assert result.chunks == 3


async def test_deleting_a_message_deletes_its_index(
    mail: MailData, embedder: FakeEmbedder, search_settings: SearchSettings
) -> None:
    mailbox = await mail.mailbox(await mail.user())
    message_id = await mail.message(mailbox, "Text to forget.")
    await _index(mail, embedder, search_settings, message_id)

    await mail.session.execute(delete(Message).where(Message.id == message_id))

    assert await _chunks(mail.session, message_id) == []
    assert await _embedding_models(mail.session) == {}


async def test_unknown_message_is_ignored(
    mail: MailData, embedder: FakeEmbedder, search_settings: SearchSettings
) -> None:
    result = await index_message(
        mail.session,
        uuid.uuid4(),
        embedder=embedder,
        storage=mail.storage,
        settings=search_settings,
    )

    assert result.chunks == 0


# --- embeddings: failures, model switch, dimensions ----------------------------------


async def test_chunks_are_stored_when_embedding_fails_and_filled_in_later(
    mail: MailData, embedder: FakeEmbedder, search_settings: SearchSettings
) -> None:
    owner = await mail.user()
    message_id = await mail.message(await mail.mailbox(owner), "Our flight leaves at nine.")
    embedder.fail = True

    result = await index_message(
        mail.session, message_id, embedder=embedder, storage=mail.storage, settings=search_settings
    )

    assert (result.chunks, result.embedded) == (1, False)
    assert await _embedding_models(mail.session) == {}
    # Full text works right away, also when the query cannot be embedded.
    hits = await search(mail.session, owner, "flight", embedder=embedder, settings=search_settings)
    assert [hit.message_id for hit in hits] == [message_id]

    embedder.fail = False
    filled = await service.fill_embeddings(mail.session, embedder, search_settings)

    assert (filled.embedded, filled.remaining, filled.switched) == (1, False, False)
    assert await _embedding_models(mail.session) == {"fake-a": 1}


async def test_wrong_dimension_is_deferred(
    mail: MailData, embedder: FakeEmbedder, search_settings: SearchSettings
) -> None:
    mailbox = await mail.mailbox(await mail.user())
    message_id = await mail.message(mailbox, "Some text.")

    async def short(texts: object, *, model: str | None = None) -> list[list[float]]:
        return [[1.0, 0.0]]

    embedder.embed = short  # type: ignore[method-assign]

    result = await index_message(
        mail.session, message_id, embedder=embedder, storage=mail.storage, settings=search_settings
    )

    assert (result.chunks, result.embedded) == (1, False)


async def test_model_switch_keeps_old_index_active_until_complete(
    mail: MailData, embedder: FakeEmbedder, search_settings: SearchSettings
) -> None:
    owner = await mail.user()
    mailbox = await mail.mailbox(owner)
    trip = await mail.message(mailbox, "Your trip: boarding starts at gate 12.")
    lunch = await mail.message(mailbox, "Pizza at noon?")
    await _index(mail, embedder, search_settings, trip, lunch)

    # The configured embedding model changes.
    embedder.model = "fake-b"
    new = await mail.message(mailbox, "Airport transfer booked.")
    await _index(mail, embedder, search_settings, new)
    # New messages get vectors of both models during the switch.
    assert await _embedding_models(mail.session) == {"fake-a": 3, "fake-b": 1}

    hits = await search(mail.session, owner, "flight", embedder=embedder, settings=search_settings)
    assert {hit.message_id for hit in hits if hit.vector_rank is not None} >= {trip, new}
    assert ("fake-a", 1) in embedder.calls  # the query used the active (old) model

    settings = search_settings.model_copy(update={"reembed_batch_size": 1})
    first = await service.fill_embeddings(mail.session, embedder, settings)
    assert (first.embedded, first.remaining, first.switched) == (1, True, False)
    assert await service.active_model(mail.session) == "fake-a"

    second = await service.fill_embeddings(mail.session, embedder, settings)
    assert (second.embedded, second.remaining, second.switched) == (1, False, True)
    assert await service.active_model(mail.session) == "fake-b"
    assert await _embedding_models(mail.session) == {"fake-b": 3}

    embedder.calls.clear()
    hits = await search(mail.session, owner, "flight", embedder=embedder, settings=search_settings)
    assert hits[0].message_id in {trip, new}
    assert embedder.calls == [("fake-b", 1)]


async def test_resize_changes_the_vector_column(
    mail: MailData, embedder: FakeEmbedder, search_settings: SearchSettings
) -> None:
    mailbox = await mail.mailbox(await mail.user())
    await _index(mail, embedder, search_settings, await mail.message(mailbox, "Text."))

    await service.resize_embeddings(mail.session, 8, "other-model")

    assert await service.embedding_dimensions(mail.session) == 8
    assert await _embedding_models(mail.session) == {}
    assert await service.active_model(mail.session) == "other-model"
    status = await service.index_status(mail.session)
    assert (status.chunks, status.dimensions, status.embeddings) == (1, 8, {})


# --- search: hybrid ranking, filters, access control ---------------------------------


async def test_hybrid_ranking_combines_full_text_and_vectors(
    mail: MailData, embedder: FakeEmbedder, search_settings: SearchSettings
) -> None:
    owner = await mail.user()
    mailbox = await mail.mailbox(owner)
    # Exact word and topic: found by both indexes.
    both = await mail.message(mailbox, "The invoice for September is attached.")
    # Same topic, other words: only the vector index finds it.
    semantic = await mail.message(mailbox, "Please confirm the payment by Friday.")
    # The word in another sense, other topic: only full text finds it.
    lexical = await mail.message(mailbox, "Pizza and football tonight, no invoice talk.")
    unrelated = await mail.message(mailbox, "Lunch at the new restaurant?")
    await _index(mail, embedder, search_settings, both, semantic, lexical, unrelated)

    hits = await search(mail.session, owner, "invoice", embedder=embedder, settings=search_settings)

    ranking = [hit.message_id for hit in hits]
    assert ranking[0] == both
    assert {semantic, lexical} <= set(ranking[1:3])
    first = hits[0]
    assert first.text_rank is not None and first.vector_rank is not None
    by_id = {hit.message_id: hit for hit in hits}
    assert by_id[semantic].text_rank is None and by_id[semantic].vector_rank is not None
    assert by_id[lexical].text_rank is not None
    assert all(hits[i].score >= hits[i + 1].score for i in range(len(hits) - 1))

    text_only = await search(
        mail.session, owner, "invoice", embedder=None, settings=search_settings
    )
    assert {hit.message_id for hit in text_only} == {both, lexical}


async def test_language_aware_full_text(
    mail: MailData, embedder: FakeEmbedder, search_settings: SearchSettings
) -> None:
    owner = await mail.user()
    mailbox = await mail.mailbox(owner)
    german = await mail.message(mailbox, "Die Rechnungen kommen morgen.", language="de")
    english = await mail.message(mailbox, "The meetings were moved.", language="en")
    await _index(mail, embedder, search_settings, german, english)

    async def find(query: str) -> list[uuid.UUID]:
        hits = await search(mail.session, owner, query, embedder=None, settings=search_settings)
        return [hit.message_id for hit in hits]

    assert await find("Rechnung") == [german]
    assert await find("meeting") == [english]
    assert await find('"kommen morgen"') == [german]
    assert await find("the") == []  # stop words only


async def test_per_message_keeps_the_best_chunk(
    mail: MailData, embedder: FakeEmbedder, search_settings: SearchSettings
) -> None:
    owner = await mail.user()
    long_body = "\n\n".join(["Invoice details follow. " * 10] * 6)
    message_id = await mail.message(await mail.mailbox(owner), long_body)
    await _index(mail, embedder, search_settings, message_id)

    all_chunks = await search(
        mail.session, owner, "invoice", embedder=embedder, settings=search_settings
    )
    per_message = await search(
        mail.session, owner, "invoice", embedder=embedder, settings=search_settings,
        per_message=True,
    )  # fmt: skip

    assert len(all_chunks) > 1
    assert [hit.chunk_id for hit in per_message] == [all_chunks[0].chunk_id]


async def test_filters(
    mail: MailData, embedder: FakeEmbedder, search_settings: SearchSettings
) -> None:
    owner = await mail.user()
    box_a, box_b = await mail.mailbox(owner), await mail.mailbox(owner)
    inbox = await mail.folder(box_a, "INBOX")
    old = await mail.message(
        box_a,
        "Invoice from last year",
        sent_at=datetime(2025, 3, 1, tzinfo=UTC),
        sender={"name": "Max Muster", "address": "max@shop.example.com"},
        folders=[inbox],
    )
    recent = await mail.message(
        box_b,
        "Invoice for this month",
        sent_at=datetime(2026, 9, 1, tzinfo=UTC),
        attachments=[("invoice.txt", "text/plain", b"Invoice total 100_% due")],
    )
    await _index(mail, embedder, search_settings, old, recent)

    async def find(filters: SearchFilters) -> set[uuid.UUID]:
        hits = await search(
            mail.session, owner, "invoice", filters, embedder=embedder, settings=search_settings
        )
        return {hit.message_id for hit in hits}

    assert await find(SearchFilters()) == {old, recent}
    assert await find(SearchFilters(mailbox_ids=[box_b])) == {recent}
    assert await find(SearchFilters(folder_ids=[inbox.id])) == {old}
    assert await find(SearchFilters(since=datetime(2026, 1, 1, tzinfo=UTC))) == {recent}
    assert await find(SearchFilters(until=datetime(2026, 1, 1, tzinfo=UTC))) == {old}
    assert await find(SearchFilters(sender="SHOP.example")) == {old}
    assert await find(SearchFilters(sender="muster")) == {old}
    assert await find(SearchFilters(sender="%")) == set()
    assert await find(SearchFilters(source="attachment")) == {recent}


async def test_search_only_returns_readable_mailboxes(
    mail: MailData, embedder: FakeEmbedder, search_settings: SearchSettings
) -> None:
    alice, bob = await mail.user(), await mail.user()
    alice_box, bob_box = await mail.mailbox(alice), await mail.mailbox(bob)
    shared_box = await mail.mailbox(None)
    alice_mail = await mail.message(alice_box, "Invoice for Alice")
    bob_mail = await mail.message(bob_box, "Invoice for Bob")
    shared_mail = await mail.message(shared_box, "Invoice in the shared mailbox")
    await _index(mail, embedder, search_settings, alice_mail, bob_mail, shared_mail)

    async def find(user: uuid.UUID, filters: SearchFilters | None = None) -> set[uuid.UUID]:
        hits = await search(
            mail.session, user, "invoice", filters, embedder=embedder, settings=search_settings
        )
        return {hit.message_id for hit in hits}

    assert await find(alice) == {alice_mail}
    assert await find(bob) == {bob_mail}
    # Asking for someone else's mailbox explicitly does not widen access.
    assert await find(bob, SearchFilters(mailbox_ids=[alice_box, shared_box])) == set()
    # Shared mailboxes are not readable until their assignment exists (#34).
    assert await find(uuid.uuid4()) == set()


async def test_empty_query(
    mail: MailData, embedder: FakeEmbedder, search_settings: SearchSettings
) -> None:
    owner = await mail.user()
    assert (
        await search(mail.session, owner, "  ", embedder=embedder, settings=search_settings) == []
    )


async def test_storage_fixture_is_isolated(storage: AttachmentStorage) -> None:
    assert storage.root.exists()
