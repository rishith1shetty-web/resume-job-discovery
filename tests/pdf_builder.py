"""Minimal PDF writer for tests.

Test fixtures previously used PyMuPDF to *build* sample PDFs. That dependency
is gone, and pdfplumber only reads, so the suite generates its own PDFs here.

Deliberately hand-rolled rather than adding a library such as reportlab: this
is ~60 lines, has no dependencies, produces byte-identical output for the same
input, and exercises the real pdfminer parsing path.

Only what the tests need is supported: Helvetica text and blank pages.
"""

from __future__ import annotations


def _escape(text: str) -> str:
    """Escape the three characters that are special inside a PDF string."""
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def build_text_pdf(pages: list[list[str]]) -> bytes:
    """Build a PDF containing selectable text.

    ``pages`` is a list of pages, each a list of lines. A page with no lines
    produces a blank page - which is how the scanned/image-only case is
    simulated.
    """
    if not pages:
        pages = [[]]

    page_count = len(pages)
    # Object ids: 1 catalog, 2 page tree, 3 font, then (page, content) pairs.
    page_ids = [4 + 2 * i for i in range(page_count)]
    content_ids = [5 + 2 * i for i in range(page_count)]

    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: (
            "<< /Type /Pages /Kids [{kids}] /Count {count} >>".format(
                kids=" ".join(f"{pid} 0 R" for pid in page_ids),
                count=page_count,
            ).encode("ascii")
        ),
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }

    for index, lines in enumerate(pages):
        page_id, content_id = page_ids[index], content_ids[index]

        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> "
            f"/Contents {content_id} 0 R >>"
        ).encode("ascii")

        # BT/ET wrap a text object; TL sets leading and T* moves down a line.
        operators = ["BT", "/F1 9 Tf", "50 750 Td", "12 TL"]
        for line in lines:
            operators.append(f"({_escape(line)}) Tj")
            operators.append("T*")
        operators.append("ET")

        stream = "\n".join(operators).encode("latin-1", "replace")
        objects[content_id] = (
            b"<< /Length "
            + str(len(stream)).encode("ascii")
            + b" >>\nstream\n"
            + stream
            + b"\nendstream"
        )

    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}

    for object_id in sorted(objects):
        offsets[object_id] = len(out)
        out += f"{object_id} 0 obj\n".encode("ascii")
        out += objects[object_id]
        out += b"\nendobj\n"

    # The cross-reference table must record each object's exact byte offset.
    xref_offset = len(out)
    highest_id = max(objects)

    out += f"xref\n0 {highest_id + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for object_id in range(1, highest_id + 1):
        out += f"{offsets[object_id]:010d} 00000 n \n".encode("ascii")

    out += (
        f"trailer\n<< /Size {highest_id + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n"
    ).encode("ascii")

    return bytes(out)


def build_pdf_from_text(text: str, *, lines_per_page: int = 50) -> bytes:
    """Paginate plain text into a PDF."""
    lines = text.split("\n")
    pages = [
        lines[start : start + lines_per_page]
        for start in range(0, len(lines), lines_per_page)
    ] or [[]]
    return build_text_pdf(pages)


def build_blank_pdf(page_count: int = 1) -> bytes:
    """A PDF with pages but no selectable text, like a scan."""
    return build_text_pdf([[] for _ in range(page_count)])
