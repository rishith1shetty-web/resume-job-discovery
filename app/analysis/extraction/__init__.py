"""Extraction registry.

Resolves a content type or file extension to its extractor, so the pipeline
calls ``extract_text(data, content_type)`` and never branches on format.
"""

from __future__ import annotations

from app.analysis.extraction.base import (
    ExtractedText,
    ExtractionError,
    TextExtractor,
)
from app.analysis.extraction.docx_extractor import DOCX_CONTENT_TYPE, DocxTextExtractor
from app.analysis.extraction.pdf_extractor import PdfTextExtractor
from app.analysis.extraction.txt_extractor import TxtTextExtractor

EXTRACTORS: tuple[TextExtractor, ...] = (
    PdfTextExtractor(),
    DocxTextExtractor(),
    TxtTextExtractor(),
)

_BY_CONTENT_TYPE: dict[str, TextExtractor] = {
    content_type: extractor
    for extractor in EXTRACTORS
    for content_type in extractor.content_types
}
_BY_EXTENSION: dict[str, TextExtractor] = {
    extension: extractor
    for extractor in EXTRACTORS
    for extension in extractor.extensions
}


def get_extractor(
    *, content_type: str | None = None, extension: str | None = None
) -> TextExtractor:
    """Resolve an extractor, preferring content type over extension."""
    if content_type:
        extractor = _BY_CONTENT_TYPE.get(content_type.split(";")[0].strip().lower())
        if extractor is not None:
            return extractor

    if extension:
        extractor = _BY_EXTENSION.get(extension.lower())
        if extractor is not None:
            return extractor

    raise ExtractionError(
        f"No extractor for content type {content_type!r} / extension {extension!r}.",
        content_type=content_type,
    )


def extract_text(
    data: bytes, *, content_type: str | None = None, extension: str | None = None
) -> ExtractedText:
    """Extract normalised text from resume bytes."""
    return get_extractor(content_type=content_type, extension=extension).extract(data)


__all__ = [
    "DOCX_CONTENT_TYPE",
    "EXTRACTORS",
    "DocxTextExtractor",
    "ExtractedText",
    "ExtractionError",
    "PdfTextExtractor",
    "TextExtractor",
    "TxtTextExtractor",
    "extract_text",
    "get_extractor",
]
