"""DOCX text extraction via python-docx."""

from __future__ import annotations

import importlib.metadata
from io import BytesIO
from typing import ClassVar

import docx
from docx.document import Document as DocxDocument
from docx.table import Table
from docx.text.paragraph import Paragraph

from app.analysis.extraction.base import ExtractedText, ExtractionError, TextExtractor

DOCX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def _iter_block_text(document: DocxDocument) -> list[str]:
    """Collect paragraph and table text.

    Tables matter: many resumes lay out skills and dates in tables, and
    iterating ``document.paragraphs`` alone silently drops all of it.
    """
    lines: list[str] = []

    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if text:
            lines.append(text)

    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            # Deduplicate horizontally merged cells, which python-docx repeats.
            deduped: list[str] = []
            for cell in cells:
                if cell and (not deduped or deduped[-1] != cell):
                    deduped.append(cell)
            if deduped:
                lines.append(" | ".join(deduped))

    return lines


class DocxTextExtractor(TextExtractor):
    content_types: ClassVar[tuple[str, ...]] = (DOCX_CONTENT_TYPE,)
    extensions: ClassVar[tuple[str, ...]] = (".docx",)
    name: ClassVar[str] = "python-docx"

    def _extract(self, data: bytes) -> ExtractedText:
        try:
            document = docx.Document(BytesIO(data))
        except Exception as exc:  # noqa: BLE001 - library raises broad types
            raise ExtractionError(
                "DOCX could not be opened; the file may be corrupt or is not a "
                "valid .docx document. Legacy .doc files are not supported.",
                content_type=DOCX_CONTENT_TYPE,
            ) from exc

        lines = _iter_block_text(document)
        text = "\n".join(lines)

        if not text.strip():
            raise ExtractionError(
                "No text found in the DOCX document.",
                content_type=DOCX_CONTENT_TYPE,
            )

        try:
            version = importlib.metadata.version("python-docx")
        except importlib.metadata.PackageNotFoundError:  # pragma: no cover
            version = "unknown"

        return ExtractedText(
            text=text,
            extractor=self.name,
            extractor_version=version,
            page_count=None,  # DOCX has no fixed pagination
        )


__all__ = ["DOCX_CONTENT_TYPE", "DocxTextExtractor", "Paragraph", "Table"]
