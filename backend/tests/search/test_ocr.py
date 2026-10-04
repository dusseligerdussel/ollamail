"""OCR of scanned attachments (#98): detection while indexing, the isolated Tesseract run,
the ``ocr`` queue job and the "attachment (OCR)" source.

All scans are synthetic: text rendered to pixels in the test (``documents.make_scan``).
Tests that need the ``tesseract`` binary with German and English data are skipped where it
is missing; CI installs it.
"""

import os
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import SearchSettings, Settings, StorageSettings
from app.mail.models import Attachment
from app.mail.storage import AttachmentStorage
from app.processing.steps import registry
from app.processing.tasks import _step_lock, enqueue_processing
from app.search import _extract_child as child
from app.search import service, tasks
from app.search.extract import Extraction, Kind, Ocr, detect_kind, extract_text
from app.search.models import ChunkSource, SearchChunk, SearchEmbedding
from app.search.service import SearchFilters, index_message, search
from app.worker import app
from tests.processing.conftest import Pipeline, run_worker
from tests.search.conftest import FakeEmbedder, MailData
from tests.search.documents import make_pdf, make_scan, make_scan_pdf


def _tesseract_ready() -> bool:
    if shutil.which("tesseract") is None:
        return False
    langs = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True).stdout
    return {"deu", "eng"} <= set(langs.split())


def _tesseract_running() -> bool:
    for cmdline in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            if cmdline.read_bytes().startswith(b"tesseract\0stdin"):
                return True
        except OSError:
            continue
    return False


# CI sets OLLAMAIL_TEST_REQUIRE_TESSERACT: there a missing Tesseract fails the tests.
needs_tesseract = pytest.mark.skipif(
    not _tesseract_ready() and not os.environ.get("OLLAMAIL_TEST_REQUIRE_TESSERACT"),
    reason="tesseract with deu+eng is not installed (apt-get install tesseract-ocr-deu)",
)

OCR_TEXT = "Cover\n\nDie Rechnung Nr. 4711 über die Wartung ist bis Monatsende zu bezahlen."
INVOICE_LINES = ["Rechnung Nr. 4711", "Betrag 120,00 EUR", "Invoice due in 14 days"]


@pytest.fixture
def settings() -> SearchSettings:
    return SearchSettings(extraction_timeout=20, ocr_timeout=60)


# --- kinds and settings --------------------------------------------------------------


@pytest.mark.parametrize(
    ("content_type", "filename"),
    [
        ("image/png", None),
        ("image/jpeg", "scan.jpg"),
        ("image/tiff", None),
        ("application/octet-stream", "Scan.TIFF"),
        ("application/octet-stream", "photo.jpeg"),
    ],
)
def test_images_are_detected(content_type: str, filename: str | None) -> None:
    assert detect_kind(content_type, filename) is Kind.IMAGE


def test_ocr_settings_are_validated() -> None:
    assert SearchSettings().ocr_mode == "pdf"
    assert SearchSettings(ocr_languages="deu+eng+fra").ocr_languages == "deu+eng+fra"
    for languages in ("deu eng", "deu;rm -rf", "-l", ""):
        with pytest.raises(ValueError, match="ocr_languages"):
            SearchSettings(ocr_languages=languages)
    with pytest.raises(ValueError, match="ocr_mode"):
        SearchSettings(ocr_mode="images")  # type: ignore[arg-type]


# --- child process logic (no Tesseract needed) ---------------------------------------


class FakeOcr(child.Ocr):
    """Records what would be sent to Tesseract and answers with a fixed text."""

    def __init__(self, mode: str = "run", max_pages: int = 20) -> None:
        super().__init__(mode, max_pages, "deu+eng")
        self.images: list[bytes] = []

    def recognise(self, image: bytes, dpi: int | None = None) -> str:
        self.images.append(image)
        self.pages += 1
        return f"recognised page {self.pages}"


def test_only_pages_without_text_are_recognised() -> None:
    data = make_scan_pdf("Cover letter with a text layer", INVOICE_LINES, INVOICE_LINES)
    ocr = FakeOcr()

    text = child.extract_pdf(data, 10_000, ocr)

    assert text == "Cover letter with a text layer\n\nrecognised page 1\n\nrecognised page 2"
    # Pages are rendered to greyscale PGM at 300 dpi (A5: 5.8 x 8.3 inch).
    header = ocr.images[0].split(b"\n", 3)
    assert header[0] == b"P5" and header[2] == b"255"
    width, height = (int(v) for v in header[1].split())
    assert abs(width - 1740) <= 2 and abs(height - 2490) <= 2
    assert len(header[3]) == width * height


def test_page_limit() -> None:
    data = make_scan_pdf(["one"], ["two"], ["three"])
    ocr = FakeOcr(max_pages=2)

    text = child.extract_pdf(data, 10_000, ocr)

    assert ocr.pages == 2
    assert text.startswith("recognised page 1\n\nrecognised page 2")


def test_blank_pages_without_images_are_not_recognised() -> None:
    ocr = FakeOcr()

    assert child.extract_pdf(make_pdf("Text", ""), 10_000, ocr).strip() == "Text"
    assert ocr.pages == 0


def test_detect_mode_only_reports_scanned_pages() -> None:
    ocr = FakeOcr(mode="detect")

    text = child.extract_pdf(make_scan_pdf("Text layer", INVOICE_LINES), 10_000, ocr)

    assert (text.strip(), ocr.needed, ocr.pages) == ("Text layer", True, 0)


def test_huge_pages_are_rendered_smaller() -> None:
    import pypdfium2

    document = pypdfium2.PdfDocument.new()
    document.new_page(14400, 14400)  # 200 x 200 inch

    image = child.render_pgm(document, 0)

    width, height = (int(v) for v in image.split(b"\n", 2)[1].split())
    assert width * height <= child.MAX_RENDER_PIXELS


def test_missing_tesseract(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(child.subprocess, "run", run)

    with pytest.raises(child.OcrUnavailable):
        child.Ocr("run", 1, "deu").recognise(b"image")


# --- isolated extraction -------------------------------------------------------------


async def test_detect_reports_scans_and_keeps_the_text_layer(settings: SearchSettings) -> None:
    data = make_scan_pdf("Cover letter with a text layer", INVOICE_LINES)

    assert await extract_text(data, Kind.PDF, settings, Ocr.DETECT) == Extraction(
        "ocr_pending", "Cover letter with a text layer"
    )
    assert await extract_text(make_scan_pdf(INVOICE_LINES), Kind.PDF, settings, Ocr.DETECT) == (
        Extraction("ocr_pending")
    )
    assert await extract_text(data, Kind.PDF, settings, Ocr.OFF) == Extraction(
        "ok", "Cover letter with a text layer"
    )


async def test_images_need_ocr(settings: SearchSettings) -> None:
    image = make_scan("Hello")

    assert await extract_text(image, Kind.IMAGE, settings, Ocr.DETECT) == Extraction("ocr_pending")
    assert await extract_text(image, Kind.IMAGE, settings) == Extraction("unsupported")


@needs_tesseract
async def test_scanned_pdf_is_recognised(settings: SearchSettings) -> None:
    data = make_scan_pdf("Cover letter with a text layer", INVOICE_LINES)

    result = await extract_text(data, Kind.PDF, settings, Ocr.RUN)

    assert (result.status, result.ocr) == ("ok", True)
    assert result.text is not None
    assert result.text.startswith("Cover letter with a text layer\n\n")
    for line in INVOICE_LINES:
        assert line in result.text


@needs_tesseract
@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "TIFF"])
async def test_images_are_recognised(settings: SearchSettings, fmt: str) -> None:
    result = await extract_text(make_scan(*INVOICE_LINES, fmt=fmt), Kind.IMAGE, settings, Ocr.RUN)

    assert (result.status, result.ocr) == ("ok", True)
    assert result.text is not None and "Rechnung Nr. 4711" in result.text


@needs_tesseract
async def test_text_pdf_is_not_marked_as_ocr(settings: SearchSettings) -> None:
    result = await extract_text(make_pdf("Plain text"), Kind.PDF, settings, Ocr.RUN)

    assert result == Extraction("ok", "Plain text")


@needs_tesseract
async def test_broken_image_is_only_a_status(settings: SearchSettings) -> None:
    result = await extract_text(b"\x89PNG\r\n\x1a\nbroken", Kind.IMAGE, settings, Ocr.RUN)

    assert result == Extraction("ocr_failed")


@needs_tesseract
async def test_slow_ocr_is_killed_with_tesseract() -> None:
    settings = SearchSettings(ocr_timeout=0.5)
    pages = [INVOICE_LINES * 8 for _ in range(6)]

    result = await extract_text(make_scan_pdf(*pages), Kind.PDF, settings, Ocr.RUN)

    assert result == Extraction("timeout")
    # The process group is killed: no Tesseract keeps running.
    assert not _tesseract_running()


# --- index and OCR job ---------------------------------------------------------------


async def _chunks(session: AsyncSession, message_id: uuid.UUID) -> list[SearchChunk]:
    return list(
        await session.scalars(
            select(SearchChunk)
            .where(SearchChunk.message_id == message_id)
            .order_by(SearchChunk.ordinal)
        )
    )


async def _attachment_id(session: AsyncSession, message_id: uuid.UUID) -> uuid.UUID:
    return (
        await session.scalars(select(Attachment.id).where(Attachment.message_id == message_id))
    ).one()


@pytest.mark.db
async def test_index_reports_scans_and_keeps_text_layer(
    mail: MailData, embedder: FakeEmbedder, search_settings: SearchSettings
) -> None:
    owner = await mail.user()
    mailbox = await mail.mailbox(owner)
    message_id = await mail.message(
        mailbox,
        "Scan attached.",
        attachments=[
            ("scan.pdf", "application/pdf", make_scan_pdf("Cover letter", INVOICE_LINES)),
            ("photo.png", "image/png", make_scan("Photo")),
        ],
    )

    result = await index_message(
        mail.session, message_id, embedder=embedder, storage=mail.storage, settings=search_settings
    )

    assert dict(result.extraction) == {"ocr_pending": 1, "unsupported": 1}
    scan_id = (
        await mail.session.scalars(
            select(Attachment.id).where(
                Attachment.message_id == message_id, Attachment.filename == "scan.pdf"
            )
        )
    ).one()
    assert result.ocr_pending == [scan_id]
    chunks = await _chunks(mail.session, message_id)
    assert [(c.source, c.content) for c in chunks] == [
        ("body", "Scan attached."),
        ("attachment", "Cover letter"),
    ]


@pytest.mark.db
@pytest.mark.parametrize(
    ("mode", "filename", "content_type", "inline", "pending"),
    [
        ("off", "scan.pdf", "application/pdf", False, False),
        ("pdf", "scan.png", "image/png", False, False),
        ("all", "scan.png", "image/png", False, True),
        ("all", "logo.png", "image/png", True, False),
    ],
)
async def test_ocr_mode(
    mail: MailData,
    embedder: FakeEmbedder,
    mode: str,
    filename: str,
    content_type: str,
    inline: bool,
    pending: bool,
) -> None:
    data = make_scan_pdf(INVOICE_LINES) if content_type == "application/pdf" else make_scan("x")
    message_id = await mail.message(
        await mail.mailbox(await mail.user()), "", attachments=[(filename, content_type, data)]
    )
    attachment = await mail.session.get(Attachment, await _attachment_id(mail.session, message_id))
    assert attachment is not None
    attachment.is_inline = inline
    settings = SearchSettings(ocr_mode=mode)  # type: ignore[arg-type]

    result = await index_message(
        mail.session, message_id, embedder=embedder, storage=mail.storage, settings=settings
    )

    assert bool(result.ocr_pending) is pending
    ocr = await service.ocr_attachment(
        mail.session, attachment.id, storage=mail.storage, settings=settings
    )
    if not pending:
        assert ocr == service.OcrResult("disabled")


@pytest.mark.db
async def test_ocr_replaces_the_attachment_chunks(
    mail: MailData,
    embedder: FakeEmbedder,
    search_settings: SearchSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = await mail.user()
    message_id = await mail.message(
        await mail.mailbox(owner),
        "Scan attached.",
        subject="Invoice",
        attachments=[("scan.pdf", "application/pdf", make_scan_pdf("Cover", INVOICE_LINES))],
    )
    await index_message(
        mail.session, message_id, embedder=embedder, storage=mail.storage, settings=search_settings
    )
    attachment_id = await _attachment_id(mail.session, message_id)
    calls: list[Ocr] = []

    async def fake_extract(data: bytes, kind: Kind, settings: SearchSettings, ocr: Ocr) -> object:
        calls.append(ocr)
        return Extraction("ok", OCR_TEXT, ocr=True)

    monkeypatch.setattr(service, "extract_text", fake_extract)
    # Everything embedded so far: ``fill_embeddings`` would skip its scan.
    await service.fill_embeddings(mail.session, embedder, search_settings)

    for _ in range(2):  # idempotent
        result = await service.ocr_attachment(
            mail.session, attachment_id, storage=mail.storage, settings=search_settings
        )

    assert result == service.OcrResult("ok", 1)
    assert calls == [Ocr.RUN, Ocr.RUN]
    chunks = await _chunks(mail.session, message_id)
    assert [(c.source, c.ordinal, c.content) for c in chunks] == [
        ("body", 0, "Scan attached."),
        ("attachment_ocr", 1, OCR_TEXT),
    ]
    assert chunks[1].heading.endswith("Subject: Invoice\nAttachment: scan.pdf")
    assert chunks[1].ts_config == "german"
    # No vector yet: ``fill_embeddings`` adds it; full-text search finds the chunk now.
    vectors = await mail.session.scalar(
        select(SearchEmbedding.id).where(SearchEmbedding.chunk_id == chunks[1].id)
    )
    assert vectors is None
    hits = await search(
        mail.session,
        owner,
        "Wartung",
        SearchFilters(source=ChunkSource.ATTACHMENT),
        embedder=None,
        settings=search_settings,
    )
    assert [(hit.chunk_id, hit.source) for hit in hits] == [(chunks[1].id, "attachment_ocr")]
    # The OCR announced its chunk, so the next run looks for it.
    filled = await service.fill_embeddings(mail.session, embedder, search_settings)
    assert (filled.embedded, filled.remaining) == (1, False)


@pytest.mark.db
async def test_failed_ocr_keeps_the_text_layer(
    mail: MailData,
    embedder: FakeEmbedder,
    search_settings: SearchSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message_id = await mail.message(
        await mail.mailbox(await mail.user()),
        "",
        attachments=[("scan.pdf", "application/pdf", make_scan_pdf("Cover", INVOICE_LINES))],
    )
    await index_message(
        mail.session, message_id, embedder=embedder, storage=mail.storage, settings=search_settings
    )

    async def fake_extract(*args: object) -> Extraction:
        return Extraction("timeout")

    monkeypatch.setattr(service, "extract_text", fake_extract)

    result = await service.ocr_attachment(
        mail.session,
        await _attachment_id(mail.session, message_id),
        storage=mail.storage,
        settings=search_settings,
    )

    assert result == service.OcrResult("timeout")
    assert [c.content for c in await _chunks(mail.session, message_id)][1:] == ["Cover"]


@pytest.mark.db
async def test_ocr_of_a_deleted_attachment(mail: MailData) -> None:
    result = await service.ocr_attachment(
        mail.session, uuid.uuid4(), storage=mail.storage, settings=SearchSettings()
    )

    assert result == service.OcrResult("gone")


def test_ocr_job_is_registered_on_its_own_queue() -> None:
    task = app.tasks["search.ocr_attachment"]
    message_id = uuid.uuid4()

    assert task.queue == "ocr"
    # Same lock as the message's ``index`` step job: OCR starts after it has committed.
    assert tasks._index_lock(message_id) == _step_lock(message_id, "index")


@pytest.fixture
def ocr_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    settings = Settings(
        storage=StorageSettings(data_dir=tmp_path), search=SearchSettings(ocr_timeout=60)
    )
    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    yield settings


@pytest.mark.db
@needs_tesseract
async def test_pipeline_recognises_a_scan_end_to_end(
    indexed: Pipeline, fake_embedder: FakeEmbedder, ocr_settings: Settings
) -> None:
    pipeline = indexed
    storage = AttachmentStorage(ocr_settings.storage.data_dir)
    message_id = await pipeline.add_message()
    attachment_id = uuid.uuid4()
    async with pipeline.database.sessionmaker() as session:
        data = make_scan_pdf(INVOICE_LINES)
        session.add(
            Attachment(
                id=attachment_id,
                message_id=message_id,
                mailbox_id=pipeline.mailbox_id,
                filename="scan.pdf",
                content_type="application/pdf",
                size=len(data),
                sha256="0" * 64,
                storage_path=storage.write(pipeline.mailbox_id, attachment_id, data),
            )
        )
        await session.commit()
    step = registry.get("index")
    assert step is not None

    with registry.isolated(step), structlog.testing.capture_logs() as logs:
        async with app.open_async():
            await enqueue_processing(message_id)
            # Index (llm), then OCR (ocr), then the embeddings of the OCR chunks (llm).
            for _ in range(10):
                await run_worker(["default", "llm", "sync", "ocr"])
                if await pipeline.pending_jobs() == 0:
                    break
        assert await pipeline.pending_jobs() == 0, await pipeline.execute(
            "SELECT task_name, status, queue_name, attempts, scheduled_at FROM procrastinate_jobs"
        )

    async with pipeline.database.sessionmaker() as session:
        chunks = await _chunks(session, message_id)
        ocr_chunk = next(c for c in chunks if c.attachment_id == attachment_id)
        embedded = await session.scalar(
            select(SearchEmbedding.model).where(SearchEmbedding.chunk_id == ocr_chunk.id)
        )
    assert ocr_chunk.source == "attachment_ocr"
    assert "Rechnung Nr. 4711" in ocr_chunk.content
    # ``fill_embeddings`` (deferred by the OCR job) added the vector.
    assert embedded == "fake-a"
    ocr_logs = [entry for entry in logs if entry["event"] == "search_attachment_ocr"]
    assert ocr_logs == [
        {
            "event": "search_attachment_ocr",
            "log_level": "info",
            "message_id": str(message_id),
            "attachment_id": str(attachment_id),
            "status": "ok",
            "chunks": 1,
        }
    ]
    assert "4711" not in repr(logs)
