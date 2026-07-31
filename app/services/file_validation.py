"""Resume upload validation.

Defence in depth, because the filename and the declared content type both
come from the client and neither can be trusted:

  1. extension allow-list
  2. declared content-type allow-list
  3. magic-byte sniffing - the actual bytes must match the claimed format
  4. extension/content-type agreement
  5. size limit
  6. filename sanitisation before anything touches the filesystem
  7. SHA-256 checksum for duplicate detection

Error messages never include file content.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath

from app.core.constants import (
    SUPPORTED_RESUME_CONTENT_TYPES,
    SUPPORTED_RESUME_EXTENSIONS,
)
from app.core.exceptions import (
    ErrorDetail,
    PayloadTooLargeError,
    UnprocessableError,
    UnsupportedMediaTypeError,
)

DOCX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)

_EXTENSION_TO_CONTENT_TYPE: dict[str, str] = {
    ".pdf": "application/pdf",
    ".docx": DOCX_CONTENT_TYPE,
    ".txt": "text/plain",
}

# Signatures checked against the real bytes. DOCX is a ZIP container, so any
# ZIP local-file-header variant is accepted here and python-docx performs the
# structural check during extraction.
_PDF_MAGIC = b"%PDF-"
_ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")

# Filesystem-safe characters. Everything else is replaced.
_UNSAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9._\- ]")
_REPEAT_DOTS_RE = re.compile(r"\.{2,}")
_REPEAT_SPACE_RE = re.compile(r"\s+")

_MAX_FILENAME_LENGTH = 200
_FALLBACK_FILENAME = "resume"

# Reserved device names on Windows; a file so named cannot be created.
_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


@dataclass(frozen=True, slots=True)
class ValidatedUpload:
    """A resume upload that passed every check."""

    original_filename: str
    safe_filename: str
    extension: str
    content_type: str
    size_bytes: int
    checksum_sha256: str
    data: bytes


def sanitize_filename(filename: str | None) -> str:
    """Reduce a client-supplied filename to a safe basename.

    Strips directory components under both POSIX and Windows rules, so
    ``../../etc/passwd`` and ``C:\\Windows\\system32\\x.pdf`` both collapse to
    a bare name. The result is used for display only - the stored path is
    derived from the checksum, never from this value.
    """
    if not filename:
        return _FALLBACK_FILENAME

    # Normalise first so lookalike Unicode cannot smuggle a separator through.
    name = unicodedata.normalize("NFKC", filename).replace("\x00", "")
    # Strip both path flavours; a POSIX server can still receive a Windows path.
    name = PureWindowsPath(PurePosixPath(name).name).name

    name = _UNSAFE_CHARS_RE.sub("_", name)
    name = _REPEAT_DOTS_RE.sub(".", name)
    name = _REPEAT_SPACE_RE.sub(" ", name).strip(" .")

    if not name:
        return _FALLBACK_FILENAME

    stem, _, suffix = name.rpartition(".")
    if stem and stem.lower() in _WINDOWS_RESERVED:
        stem = f"{stem}_file"
        name = f"{stem}.{suffix}"

    if len(name) > _MAX_FILENAME_LENGTH:
        stem, _, suffix = name.rpartition(".")
        keep = _MAX_FILENAME_LENGTH - len(suffix) - 1
        name = f"{stem[:keep]}.{suffix}" if suffix else name[:_MAX_FILENAME_LENGTH]

    return name or _FALLBACK_FILENAME


def _extension_of(filename: str) -> str:
    _, dot, suffix = filename.rpartition(".")
    return f".{suffix.lower()}" if dot else ""


def _sniff_extension(data: bytes) -> str | None:
    """Identify the format from the bytes themselves."""
    if data.startswith(_PDF_MAGIC):
        return ".pdf"
    if any(data.startswith(magic) for magic in _ZIP_MAGICS):
        return ".docx"
    return None


# Signatures of formats that are definitely not resumes. Checked explicitly
# because latin-1 decodes any byte sequence, so a decode test alone would let
# binary files through whenever they happen to contain no NUL bytes.
_BINARY_SIGNATURES: tuple[bytes, ...] = (
    b"\x89PNG",           # PNG
    b"GIF8",              # GIF
    b"\xff\xd8\xff",      # JPEG
    b"BM",                # BMP
    b"RIFF",              # WEBP / WAV / AVI
    b"\x7fELF",           # ELF binary
    b"MZ",                # Windows executable
    b"\xd0\xcf\x11\xe0",  # legacy MS Office (.doc/.xls)
    b"%!PS",              # PostScript
    b"\x1f\x8b",          # gzip
    b"Rar!",              # RAR
    b"7z\xbc\xaf",        # 7-Zip
    b"OggS",              # Ogg
    b"\x00\x00\x01\x00",  # ICO
)

# Above this share of control characters, the data is binary, not prose.
_MAX_CONTROL_CHAR_RATIO = 0.05


def _looks_like_text(data: bytes) -> bool:
    """True when the bytes are plausibly a plain-text document."""
    sample = data[:8192]

    if b"\x00" in sample:
        return False
    if any(sample.startswith(signature) for signature in _BINARY_SIGNATURES):
        return False

    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            decoded = sample.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue

        if not decoded:
            return False

        control = sum(
            1
            for char in decoded
            if not char.isprintable() and char not in "\t\n\r\f\v"
        )
        return (control / len(decoded)) <= _MAX_CONTROL_CHAR_RATIO

    return False


def compute_checksum(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_resume_upload(
    *,
    filename: str | None,
    content_type: str | None,
    data: bytes,
    max_upload_mb: int,
) -> ValidatedUpload:
    """Validate an uploaded resume, or raise a client-facing error."""
    if not data:
        raise UnprocessableError(
            message="Uploaded file is empty.",
            details=[
                ErrorDetail(
                    code="empty_file",
                    field="file",
                    message="The uploaded file contains no data.",
                )
            ],
        )

    max_bytes = max_upload_mb * 1024 * 1024
    if len(data) > max_bytes:
        raise PayloadTooLargeError(
            message=f"Resume exceeds the {max_upload_mb} MB limit.",
            details=[
                ErrorDetail(
                    code="file_too_large",
                    field="file",
                    message=(
                        f"File is {len(data) / 1024 / 1024:.1f} MB; the limit "
                        f"is {max_upload_mb} MB."
                    ),
                )
            ],
        )

    safe_filename = sanitize_filename(filename)
    extension = _extension_of(safe_filename)
    declared = (content_type or "").split(";")[0].strip().lower()

    # Prefer the bytes; fall back to the extension for plain text, which has
    # no signature to sniff.
    sniffed = _sniff_extension(data)
    if sniffed is None and _looks_like_text(data):
        sniffed = ".txt"

    if sniffed is None:
        raise UnsupportedMediaTypeError(
            message="File content is not a supported resume format.",
            details=[
                ErrorDetail(
                    code="unrecognised_file_content",
                    field="file",
                    message=(
                        "The file content does not match PDF, DOCX or plain "
                        "text. Legacy .doc files are not supported."
                    ),
                )
            ],
        )

    # The extension is advisory; if absent or wrong, trust the sniffed type.
    if extension not in SUPPORTED_RESUME_EXTENSIONS:
        extension = sniffed
        if not safe_filename.lower().endswith(extension):
            safe_filename = f"{safe_filename}{extension}"

    if extension != sniffed:
        # A .pdf that is really a ZIP, or vice versa - refuse rather than guess.
        raise UnsupportedMediaTypeError(
            message="File extension does not match its actual content.",
            details=[
                ErrorDetail(
                    code="content_extension_mismatch",
                    field="file",
                    message=(
                        f"File is named {extension} but its content is "
                        f"{sniffed}."
                    ),
                )
            ],
        )

    resolved_content_type = _EXTENSION_TO_CONTENT_TYPE[extension]

    # A declared type that contradicts the bytes is rejected; a missing or
    # generic one (browsers often send application/octet-stream) is tolerated.
    if (
        declared
        and declared in SUPPORTED_RESUME_CONTENT_TYPES
        and declared != resolved_content_type
    ):
        raise UnsupportedMediaTypeError(
            message="Declared content type does not match the file content.",
            details=[
                ErrorDetail(
                    code="content_type_mismatch",
                    field="file",
                    message=(
                        f"Declared {declared} but the content is "
                        f"{resolved_content_type}."
                    ),
                )
            ],
        )

    return ValidatedUpload(
        original_filename=safe_filename,
        safe_filename=safe_filename,
        extension=extension,
        content_type=resolved_content_type,
        size_bytes=len(data),
        checksum_sha256=compute_checksum(data),
        data=data,
    )
