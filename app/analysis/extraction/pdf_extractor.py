"""PDF text extraction via PyMuPDF.

Licensing note: PyMuPDF is AGPL-3.0 (or commercial). That is acceptable for a
local single-user tool but becomes viral if this service is ever hosted or
distributed. Because extraction sits behind ``TextExtractor``, switching to
pdfplumber later means replacing only this file.
"""

from __future__ import annotations

from typing import ClassVar

import pymupdf

from app.analysis.extraction.base import ExtractedText, ExtractionError, TextExtractor


class PdfTextExtractor(TextExtractor):
    content_types: ClassVar[tuple[str, ...]] = ("application/pdf",)
    extensions: ClassVar[tuple[str, ...]] = (".pdf",)
    name: ClassVar[str] = "pymupdf"

    def _extract(self, data: bytes) -> ExtractedText:
        warnings: list[str] = []

        try:
            document = pymupdf.open(stream=data, filetype="pdf")
        except Exception as exc:  # noqa: BLE001 - library raises broad types
            # Deliberately does not echo the exception text, which can embed
            # fragments of document content.
            raise ExtractionError(
                "PDF could not be opened; the file may be corrupt or encrypted.",
                content_type="application/pdf",
            ) from exc

        try:
            if document.needs_pass:
                raise ExtractionError(
                    "PDF is password protected and cannot be read.",
                    content_type="application/pdf",
                )

            pages: list[str] = []
            for page in document:
                try:
                    # "text" preserves reading order well enough for the
                    # line-based section detection downstream.
                    pages.append(page.get_text("text"))
                except Exception:  # noqa: BLE001 - skip unreadable page only
                    warnings.append(f"Page {page.number + 1} could not be read.")

            page_count = document.page_count
        finally:
            document.close()

        text = "\n".join(pages)

        if not text.strip():
            # Almost always a scanned/image-only PDF. Report it plainly rather
            # than proceeding to analyse an empty document.
            raise ExtractionError(
                "No selectable text found in the PDF. The file appears to be "
                "scanned or image-only; OCR is not supported.",
                content_type="application/pdf",
            )

        return ExtractedText(
            text=text,
            extractor=self.name,
            extractor_version=getattr(pymupdf, "__version__", "unknown"),
            page_count=page_count,
            warnings=warnings,
        )
