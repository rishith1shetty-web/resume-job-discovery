"""Text-extraction contract.

Every format is handled behind this one interface so the analysis pipeline
never branches on file type, and swapping a library (for example PyMuPDF for
pdfplumber) touches exactly one adapter.

Extractors are pure: they take bytes and return text. They never write to
disk and never modify the uploaded file (CLAUDE.md section 18 - the original
resume is never altered).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar

from app.analysis.text import clean_text


class ExtractionError(Exception):
    """Raised when a document cannot be read.

    The message must never contain document content - only the reason - so
    resume text cannot leak into logs or API error responses.
    """

    def __init__(self, message: str, *, content_type: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.content_type = content_type


@dataclass(slots=True)
class ExtractedText:
    """Result of reading a resume file."""

    text: str
    extractor: str
    extractor_version: str
    page_count: int | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def char_count(self) -> int:
        return len(self.text)

    @property
    def is_empty(self) -> bool:
        """True when no usable text was recovered.

        A scanned/image-only PDF extracts to nothing; the caller must treat
        that as a failure rather than analysing an empty profile.
        """
        return not self.text.strip()


class TextExtractor(ABC):
    """Base class for format-specific extractors."""

    #: Content types this extractor handles.
    content_types: ClassVar[tuple[str, ...]] = ()
    #: File extensions this extractor handles, lowercase, including the dot.
    extensions: ClassVar[tuple[str, ...]] = ()
    #: Short identifier stored on the resume_texts row.
    name: ClassVar[str] = "base"

    @abstractmethod
    def _extract(self, data: bytes) -> ExtractedText:
        """Format-specific extraction. Implemented by subclasses."""

    def extract(self, data: bytes) -> ExtractedText:
        """Extract and normalise text from document bytes."""
        if not data:
            raise ExtractionError("File is empty.")

        result = self._extract(data)
        result.text = clean_text(result.text)
        return result
