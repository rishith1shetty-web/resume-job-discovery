"""Plain-text extraction with encoding detection."""

from __future__ import annotations

from typing import ClassVar

from app.analysis.extraction.base import ExtractedText, ExtractionError, TextExtractor

# Tried in order. utf-8-sig first so a BOM is consumed rather than becoming a
# stray character; latin-1 last because it accepts any byte and would mask a
# genuine mismatch if tried earlier.
_ENCODINGS: tuple[str, ...] = ("utf-8-sig", "utf-8", "cp1252", "latin-1")


class TxtTextExtractor(TextExtractor):
    content_types: ClassVar[tuple[str, ...]] = ("text/plain",)
    extensions: ClassVar[tuple[str, ...]] = (".txt",)
    name: ClassVar[str] = "text-decode"

    def _extract(self, data: bytes) -> ExtractedText:
        warnings: list[str] = []

        for encoding in _ENCODINGS:
            try:
                text = data.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                continue

            if encoding != _ENCODINGS[0]:
                warnings.append(f"Decoded using {encoding}.")

            if not text.strip():
                raise ExtractionError(
                    "Text file contains no readable content.",
                    content_type="text/plain",
                )

            return ExtractedText(
                text=text,
                extractor=self.name,
                extractor_version=encoding,
                page_count=None,
                warnings=warnings,
            )

        raise ExtractionError(
            "Text file could not be decoded with any supported encoding.",
            content_type="text/plain",
        )
