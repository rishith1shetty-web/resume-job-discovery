"""PDF text extraction via pdfplumber.

Chosen over PyMuPDF for distribution reasons:

* **Licence.** pdfplumber and pdfminer.six are MIT. PyMuPDF is AGPL-3.0, which
  is viral for a distributed Windows executable unless a commercial licence is
  bought.
* **Packaging.** The text path is pdfminer.six, which is pure Python and
  bundles cleanly under PyInstaller.

Only this file changed in the swap. ``TextExtractor`` and every caller are
untouched, which is the point of the interface.
"""

from __future__ import annotations

import importlib.metadata
from io import BytesIO
from typing import ClassVar

import pdfplumber
from pdfminer.pdfdocument import PDFPasswordIncorrect

from app.analysis.extraction.base import ExtractedText, ExtractionError, TextExtractor

# pdfplumber's defaults are tuned for tables. Resumes are mostly running text,
# and a slightly wider x tolerance stops letter-spaced headings ("S K I L L S")
# from being split into separate words.
_X_TOLERANCE = 2.0
_Y_TOLERANCE = 3.0


class PdfTextExtractor(TextExtractor):
    content_types: ClassVar[tuple[str, ...]] = ("application/pdf",)
    extensions: ClassVar[tuple[str, ...]] = (".pdf",)
    name: ClassVar[str] = "pdfplumber"

    def _extract(self, data: bytes) -> ExtractedText:
        warnings: list[str] = []

        try:
            document = pdfplumber.open(BytesIO(data))
        except PDFPasswordIncorrect as exc:
            raise ExtractionError(
                "PDF is password protected and cannot be read.",
                content_type="application/pdf",
            ) from exc
        except Exception as exc:  # noqa: BLE001 - library raises broad types
            # Deliberately does not echo the exception text, which can embed
            # fragments of document content.
            raise ExtractionError(
                "PDF could not be opened; the file may be corrupt or encrypted.",
                content_type="application/pdf",
            ) from exc

        try:
            pages: list[str] = []
            for index, page in enumerate(document.pages, start=1):
                try:
                    extracted = page.extract_text(
                        x_tolerance=_X_TOLERANCE, y_tolerance=_Y_TOLERANCE
                    )
                except Exception:  # noqa: BLE001 - skip unreadable page only
                    warnings.append(f"Page {index} could not be read.")
                    continue

                if extracted:
                    pages.append(extracted)

            page_count = len(document.pages)
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

        try:
            version = importlib.metadata.version("pdfplumber")
        except importlib.metadata.PackageNotFoundError:  # pragma: no cover
            version = "unknown"

        return ExtractedText(
            text=text,
            extractor=self.name,
            extractor_version=version,
            page_count=page_count,
            warnings=warnings,
        )
