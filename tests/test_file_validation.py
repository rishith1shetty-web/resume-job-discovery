"""Upload validation and local storage."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.core.exceptions import (
    PayloadTooLargeError,
    UnprocessableError,
    UnsupportedMediaTypeError,
)
from app.services.file_validation import (
    DOCX_CONTENT_TYPE,
    compute_checksum,
    sanitize_filename,
    validate_resume_upload,
)
from app.services.storage import LocalResumeStorage
from tests.factories import make_docx, make_pdf, make_txt


def _validate(data: bytes, filename: str, content_type: str | None = None):
    return validate_resume_upload(
        filename=filename,
        content_type=content_type,
        data=data,
        max_upload_mb=10,
    )


# --- Filename sanitisation --------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("resume.pdf", "resume.pdf"),
        ("../../etc/passwd", "passwd"),
        ("..\\..\\windows\\system32\\evil.pdf", "evil.pdf"),
        ("/absolute/path/cv.pdf", "cv.pdf"),
        ("C:\\Users\\me\\cv.pdf", "cv.pdf"),
        ("my resume (final).pdf", "my resume _final_.pdf"),
        ("", "resume"),
        (None, "resume"),
    ],
)
def test_sanitize_filename(raw: str | None, expected: str) -> None:
    assert sanitize_filename(raw) == expected


def test_sanitize_strips_null_bytes() -> None:
    assert "\x00" not in sanitize_filename("re\x00sume.pdf")


def test_sanitize_truncates_long_names() -> None:
    assert len(sanitize_filename("a" * 500 + ".pdf")) <= 200


def test_sanitize_avoids_windows_reserved_names() -> None:
    assert sanitize_filename("CON.pdf") != "CON.pdf"


# --- Content validation -----------------------------------------------------


def test_accepts_pdf() -> None:
    result = _validate(make_pdf(), "resume.pdf", "application/pdf")

    assert result.extension == ".pdf"
    assert result.content_type == "application/pdf"
    assert len(result.checksum_sha256) == 64


def test_accepts_docx() -> None:
    result = _validate(make_docx(), "resume.docx", DOCX_CONTENT_TYPE)

    assert result.extension == ".docx"


def test_accepts_txt() -> None:
    result = _validate(make_txt(), "resume.txt", "text/plain")

    assert result.extension == ".txt"


def test_rejects_empty_file() -> None:
    with pytest.raises(UnprocessableError, match="empty"):
        _validate(b"", "resume.pdf", "application/pdf")


def test_rejects_oversized_file() -> None:
    with pytest.raises(PayloadTooLargeError):
        validate_resume_upload(
            filename="big.txt",
            content_type="text/plain",
            data=b"x" * (2 * 1024 * 1024),
            max_upload_mb=1,
        )


def test_rejects_unrecognised_binary() -> None:
    # A PNG is neither PDF, DOCX nor decodable text.
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    with pytest.raises(UnsupportedMediaTypeError, match="not a supported"):
        _validate(png, "image.png", "image/png")


def test_rejects_extension_content_mismatch() -> None:
    # A ZIP renamed to .pdf must not be accepted on its name alone.
    with pytest.raises(UnsupportedMediaTypeError, match="does not match"):
        _validate(make_docx(), "sneaky.pdf", "application/pdf")


def test_rejects_declared_content_type_mismatch() -> None:
    with pytest.raises(UnsupportedMediaTypeError, match="Declared content type"):
        _validate(make_pdf(), "resume.pdf", "text/plain")


def test_tolerates_generic_octet_stream() -> None:
    # Browsers frequently send this; the bytes are authoritative.
    result = _validate(make_pdf(), "resume.pdf", "application/octet-stream")

    assert result.content_type == "application/pdf"


def test_infers_extension_when_filename_has_none() -> None:
    result = _validate(make_pdf(), "resume", None)

    assert result.extension == ".pdf"
    assert result.safe_filename.endswith(".pdf")


def test_checksum_is_stable_and_content_dependent() -> None:
    data = make_txt()

    assert compute_checksum(data) == compute_checksum(data)
    assert compute_checksum(data) != compute_checksum(data + b" ")


def test_validation_errors_do_not_leak_content() -> None:
    secret = b"\x89PNG\r\n\x1a\nCONFIDENTIAL-SALARY-950000"
    try:
        _validate(secret, "x.png", "image/png")
    except UnsupportedMediaTypeError as exc:
        assert "CONFIDENTIAL" not in str(exc)
        assert all("CONFIDENTIAL" not in d.message for d in exc.details)
    else:  # pragma: no cover
        pytest.fail("Expected UnsupportedMediaTypeError")


# --- Storage ----------------------------------------------------------------


def test_storage_is_content_addressed(tmp_path: Path) -> None:
    storage = LocalResumeStorage(tmp_path)
    user_id = uuid.uuid4()
    data = make_txt()
    checksum = compute_checksum(data)

    path = storage.save(
        user_id=user_id, checksum=checksum, extension=".txt", data=data
    )

    # The client filename never appears in the stored path.
    assert checksum in path
    assert storage.exists(path)
    assert storage.read(path) == data


def test_storage_never_rewrites_an_existing_file(tmp_path: Path) -> None:
    # The original upload must stay byte-for-byte intact.
    storage = LocalResumeStorage(tmp_path)
    user_id = uuid.uuid4()
    data = make_txt()
    checksum = compute_checksum(data)

    first = storage.save(
        user_id=user_id, checksum=checksum, extension=".txt", data=data
    )
    mtime = storage.resolve(first).stat().st_mtime_ns

    second = storage.save(
        user_id=user_id, checksum=checksum, extension=".txt", data=b"different"
    )

    assert first == second
    assert storage.resolve(first).stat().st_mtime_ns == mtime
    assert storage.read(first) == data


def test_storage_rejects_path_escape(tmp_path: Path) -> None:
    storage = LocalResumeStorage(tmp_path)

    with pytest.raises(ValueError, match="escapes"):
        storage.resolve("../../../etc/passwd")


def test_storage_leaves_no_partial_files(tmp_path: Path) -> None:
    storage = LocalResumeStorage(tmp_path)
    storage.save(
        user_id=uuid.uuid4(), checksum="a" * 64, extension=".txt", data=b"data"
    )

    assert not list(tmp_path.rglob("*.part"))
