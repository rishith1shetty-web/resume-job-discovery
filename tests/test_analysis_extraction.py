"""Text extraction across PDF, DOCX and TXT."""

from __future__ import annotations

import pytest

from app.analysis.extraction import ExtractionError, extract_text, get_extractor
from app.analysis.extraction.docx_extractor import DOCX_CONTENT_TYPE
from tests.factories import (
    FINANCIAL_ANALYST_RESUME,
    make_docx,
    make_pdf,
    make_scanned_pdf,
    make_txt,
)


def test_extract_txt() -> None:
    result = extract_text(make_txt(), content_type="text/plain")

    assert "Financial Analyst" in result.text
    assert result.extractor == "text-decode"
    assert result.char_count > 100
    assert not result.is_empty


def test_extract_pdf() -> None:
    result = extract_text(make_pdf(), content_type="application/pdf")

    assert "Financial Analyst" in result.text
    assert result.extractor == "pymupdf"
    assert result.page_count is not None and result.page_count >= 1


def test_extract_docx() -> None:
    result = extract_text(make_docx(), content_type=DOCX_CONTENT_TYPE)

    assert "Financial Analyst" in result.text
    assert result.extractor == "python-docx"
    # DOCX has no fixed pagination.
    assert result.page_count is None


def test_docx_extraction_includes_tables() -> None:
    # Many resumes lay skills out in tables; dropping them loses real content.
    result = extract_text(make_docx(), content_type=DOCX_CONTENT_TYPE)

    assert "Tableau" in result.text


def test_extraction_is_deterministic() -> None:
    first = extract_text(make_txt(), content_type="text/plain")
    second = extract_text(make_txt(), content_type="text/plain")

    assert first.text == second.text


def test_extractor_resolves_by_extension_when_content_type_missing() -> None:
    assert get_extractor(extension=".pdf").name == "pymupdf"
    assert get_extractor(extension=".docx").name == "python-docx"
    assert get_extractor(extension=".txt").name == "text-decode"


def test_content_type_with_charset_parameter_resolves() -> None:
    result = extract_text(make_txt(), content_type="text/plain; charset=utf-8")

    assert not result.is_empty


def test_scanned_pdf_is_rejected() -> None:
    # Image-only PDFs yield no text; analysing an empty profile would be worse
    # than failing loudly.
    with pytest.raises(ExtractionError, match="scanned or image-only"):
        extract_text(make_scanned_pdf(), content_type="application/pdf")


def test_corrupt_pdf_is_rejected() -> None:
    with pytest.raises(ExtractionError):
        extract_text(b"%PDF-1.4 not really a pdf", content_type="application/pdf")


def test_corrupt_docx_is_rejected() -> None:
    with pytest.raises(ExtractionError, match="corrupt|not a valid"):
        extract_text(b"definitely not a docx", content_type=DOCX_CONTENT_TYPE)


def test_empty_file_is_rejected() -> None:
    with pytest.raises(ExtractionError, match="empty"):
        extract_text(b"", content_type="text/plain")


def test_unknown_content_type_is_rejected() -> None:
    with pytest.raises(ExtractionError, match="No extractor"):
        extract_text(b"data", content_type="application/zip")


def test_extraction_errors_never_include_document_content() -> None:
    # Error messages surface in API responses and logs.
    secret = "CONFIDENTIAL-SALARY-950000"
    try:
        extract_text(secret.encode(), content_type="application/pdf")
    except ExtractionError as exc:
        assert secret not in str(exc)
        assert secret not in exc.message
    else:  # pragma: no cover
        pytest.fail("Expected ExtractionError")


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "cp1252", "latin-1"])
def test_txt_decodes_common_encodings(encoding: str) -> None:
    result = extract_text(
        FINANCIAL_ANALYST_RESUME.encode(encoding), content_type="text/plain"
    )

    assert "Financial Analyst" in result.text
