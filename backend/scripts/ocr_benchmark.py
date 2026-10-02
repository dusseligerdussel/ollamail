"""OCR throughput on synthetic scanned pages (pages per minute).

Usage (in ``backend/``, needs Tesseract and the dev dependencies for Pillow)::

    uv run python -m scripts.ocr_benchmark --pages 24 --concurrency 4

Renders A4 pages of invented German/English business text at 300 dpi into image-only
PDFs (one page each, like a scanner's output) and recognises them through the same
isolated extraction as the worker (``extract_text`` with ``Ocr.RUN``). ``--concurrency``
corresponds to ``OLLAMAIL_SEARCH_OCR_CONCURRENCY``. ``word_accuracy`` is the share of the
rendered words found again, in order. No real documents are used.
"""

import argparse
import asyncio
import difflib
import io
import random
import time

from app.core.config import SearchSettings
from app.search.extract import Kind, Ocr, extract_text

_WORDS = [
    "Rechnung", "Betrag", "Zahlung", "Lieferung", "Vertrag", "Angebot", "Kunde", "Leistung",
    "Datum", "Frist", "Monat", "Wartung", "Auftrag", "Position", "Menge", "Preis", "Summe",
    "Steuer", "Konto", "Bank", "Ansprechpartner", "invoice", "amount", "payment", "delivery",
    "contract", "offer", "customer", "service", "date", "due", "month", "maintenance", "order",
    "item", "quantity", "price", "total", "tax", "account", "contact", "please", "regards",
]  # fmt: skip


def _page(seed: int, dpi: int = 300) -> tuple[bytes, list[str]]:
    from PIL import Image, ImageDraw, ImageFont

    rng = random.Random(seed)
    width, height = int(8.27 * dpi), int(11.69 * dpi)
    image = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=dpi // 7)
    y = dpi
    text: list[str] = []
    while y < height - dpi:
        words = [rng.choice(_WORDS) for _ in range(9)]
        words.append(f"{rng.randint(1, 9999)},{rng.randint(0, 99):02d}")
        draw.text((dpi, y), " ".join(words), fill=0, font=font)
        text.extend(words)
        y += int(dpi / 4.5)
    out = io.BytesIO()
    image.save(out, format="PDF", resolution=float(dpi))
    return out.getvalue(), text


async def _run(pages: int, concurrency: int) -> None:
    settings = SearchSettings(ocr_timeout=600)
    queue: asyncio.Queue[tuple[bytes, list[str]]] = asyncio.Queue()
    for seed in range(pages):
        queue.put_nowait(_page(seed))
    chars = 0
    expected = 0
    matched = 0
    failures = 0

    async def worker() -> None:
        nonlocal chars, expected, matched, failures
        while not queue.empty():
            document, words = queue.get_nowait()
            result = await extract_text(document, Kind.PDF, settings, Ocr.RUN)
            expected += len(words)
            if result.status == "ok" and result.ocr and result.text:
                chars += len(result.text)
                found = result.text.split()
                blocks = difflib.SequenceMatcher(None, words, found, autojunk=False)
                matched += sum(block.size for block in blocks.get_matching_blocks())
            else:
                failures += 1

    start = time.perf_counter()
    await asyncio.gather(*(worker() for _ in range(concurrency)))
    elapsed = time.perf_counter() - start
    print(
        f"pages={pages} concurrency={concurrency} "
        f"seconds={elapsed:.1f} pages_per_minute={pages / elapsed * 60:.1f} "
        f"seconds_per_page={elapsed / pages:.2f} chars_per_page={chars // max(pages, 1)} "
        f"word_accuracy={matched / max(expected, 1):.3f} failures={failures}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--pages", type=int, default=24)
    parser.add_argument("--concurrency", type=int, default=1)
    args = parser.parse_args()
    asyncio.run(_run(args.pages, args.concurrency))


if __name__ == "__main__":
    main()
