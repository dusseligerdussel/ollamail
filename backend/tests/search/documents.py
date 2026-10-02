"""Tiny synthetic documents for extraction tests (built in code, no binary fixtures)."""

import io
import zipfile

_DOCX_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def make_pdf(*pages: str) -> bytes:
    """A minimal PDF with one line of Helvetica text per page."""
    objects: list[bytes] = []
    page_ids = [4 + 2 * i for i in range(len(pages))]
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for index, text in enumerate(pages):
        content_id = page_ids[index] + 1
        objects.append(
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]"
                f" /Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>"
            ).encode()
        )
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 72 712 Td ({escaped}) Tj ET".encode("latin-1")
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % number + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(
        b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    )
    return out.getvalue()


def make_docx(*paragraphs: str, document_xml: str | None = None) -> bytes:
    body = "".join(f'<w:p><w:r><w:t xml:space="preserve">{p}</w:t></w:r></w:p>' for p in paragraphs)
    xml = document_xml or (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{_DOCX_NS}"><w:body>{body}</w:body></w:document>'
    )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", xml)
    return out.getvalue()


def make_scan(*lines: str, fmt: str = "PNG", dpi: int = 300) -> bytes:
    """A synthetic "scanned" A5 page: black text on white, rendered to pixels (needs Pillow).

    ``fmt``: ``PNG``, ``JPEG``, ``TIFF`` or ``PDF`` (an image-only PDF without text layer).
    """
    from PIL import Image, ImageDraw, ImageFont

    width, height = int(5.8 * dpi), int(8.3 * dpi)
    image = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=dpi // 6)
    y = dpi // 2
    for line in lines:
        draw.text((dpi // 2, y), line, fill=0, font=font)
        y += dpi // 4
    out = io.BytesIO()
    image.save(out, format=fmt, resolution=float(dpi), dpi=(dpi, dpi))
    return out.getvalue()


def make_scan_pdf(*pages: list[str] | str) -> bytes:
    """A PDF of scanned pages (lists of lines) and text pages (strings, with text layer)."""
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for page in pages:
        data = make_pdf(page) if isinstance(page, str) else make_scan(*page, fmt="PDF")
        writer.add_page(PdfReader(io.BytesIO(data)).pages[0])
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
