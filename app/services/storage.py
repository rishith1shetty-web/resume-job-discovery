"""Local resume file storage.

Files are content-addressed: the stored path is derived from the user id and
the SHA-256 checksum, never from the client-supplied filename. That removes
path traversal as a concern and makes re-uploading identical bytes a no-op.

Stored files are written once and never modified (CLAUDE.md section 18: the
original resume is never altered or rewritten).

Only the local backend exists in Phase 2. The interface is deliberately narrow
so an object-storage backend can be added later without touching callers.
"""

from __future__ import annotations

import uuid
from pathlib import Path, PurePosixPath
from typing import Protocol

from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)

RESUME_SUBDIR = "resumes"

#: Storage paths are *logical keys*, not OS paths: they are persisted in
#: ``resumes.storage_path`` and must read back identically on any platform, so
#: they are always built with ``PurePosixPath`` and always use forward slashes.
#: A native ``Path`` would emit backslashes on Windows, which would make the
#: stored value platform-specific - and the same forward-slash form is what an
#: object-storage backend will want as its key.
STORAGE_SEPARATOR = "/"


class ResumeStorage(Protocol):
    """Where resume files live."""

    def save(
        self, *, user_id: uuid.UUID, checksum: str, extension: str, data: bytes
    ) -> str:
        """Persist bytes and return a storage path relative to the root."""
        ...

    def resolve(self, storage_path: str) -> Path:
        """Absolute path for a stored file."""
        ...

    def exists(self, storage_path: str) -> bool: ...

    def read(self, storage_path: str) -> bytes: ...


class LocalResumeStorage:
    """Filesystem-backed storage under ``settings.storage_dir``."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root).resolve()

    @property
    def root(self) -> Path:
        return self._root

    def _relative_path(self, user_id: uuid.UUID, checksum: str, extension: str) -> str:
        # Joined rather than interpolated, so the separator is the path
        # library's concern and cannot drift. Shard by the checksum prefix so
        # one directory does not accumulate every file a user ever uploads.
        return str(
            PurePosixPath(
                RESUME_SUBDIR,
                str(user_id),
                checksum[:2],
                f"{checksum}{extension}",
            )
        )

    def save(
        self, *, user_id: uuid.UUID, checksum: str, extension: str, data: bytes
    ) -> str:
        relative = self._relative_path(user_id, checksum, extension)
        target = self.resolve(relative)

        if target.exists():
            # Content-addressed: identical bytes are already stored. Never
            # rewrite, so the original upload stays byte-for-byte intact.
            logger.info(
                "resume_file_already_stored", checksum_prefix=checksum[:8]
            )
            return relative

        target.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temporary name then rename, so a crash mid-write cannot
        # leave a truncated file at the final path.
        temporary = target.with_suffix(target.suffix + ".part")
        temporary.write_bytes(data)
        temporary.replace(target)

        logger.info(
            "resume_file_stored",
            checksum_prefix=checksum[:8],
            size_bytes=len(data),
        )
        return relative

    def resolve(self, storage_path: str) -> Path:
        """Resolve a relative storage path, refusing anything outside the root.

        The stored key is interpreted as POSIX and rebuilt segment by segment,
        so it resolves to the same file whatever the host platform separator
        happens to be.
        """
        relative = PurePosixPath(storage_path)
        if relative.is_absolute():
            raise ValueError("Storage path escapes the storage root.")

        candidate = self._root.joinpath(*relative.parts).resolve()
        if not candidate.is_relative_to(self._root):
            # Defensive: paths are generated internally, never client-supplied.
            raise ValueError("Storage path escapes the storage root.")
        return candidate

    def exists(self, storage_path: str) -> bool:
        try:
            return self.resolve(storage_path).is_file()
        except ValueError:
            return False

    def read(self, storage_path: str) -> bytes:
        return self.resolve(storage_path).read_bytes()


def get_storage(settings: Settings) -> LocalResumeStorage:
    return LocalResumeStorage(settings.storage_dir)
