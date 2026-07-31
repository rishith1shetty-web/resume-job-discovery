"""Resume upload and retrieval."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.constants import ResumeStatus
from app.core.exceptions import ErrorDetail, NotFoundError
from app.core.logging import get_logger
from app.models.profile import ResumeProfile
from app.models.resume import Resume
from app.services.file_validation import ValidatedUpload
from app.services.storage import ResumeStorage

logger = get_logger(__name__)


async def find_by_checksum(
    session: AsyncSession, *, user_id: uuid.UUID, checksum: str
) -> Resume | None:
    result = await session.execute(
        select(Resume).where(
            Resume.user_id == user_id,
            Resume.checksum_sha256 == checksum,
        )
    )
    return result.scalar_one_or_none()


async def create_resume(
    session: AsyncSession,
    storage: ResumeStorage,
    *,
    user_id: uuid.UUID,
    upload: ValidatedUpload,
) -> tuple[Resume, bool]:
    """Store an uploaded resume.

    Returns ``(resume, created)``. Re-uploading byte-identical content returns
    the existing record with ``created=False`` rather than fragmenting that
    resume's history across duplicate rows.
    """
    existing = await find_by_checksum(
        session, user_id=user_id, checksum=upload.checksum_sha256
    )
    if existing is not None:
        logger.info(
            "resume_duplicate_upload",
            resume_id=str(existing.id),
            checksum_prefix=upload.checksum_sha256[:8],
        )
        return existing, False

    storage_path = storage.save(
        user_id=user_id,
        checksum=upload.checksum_sha256,
        extension=upload.extension,
        data=upload.data,
    )

    resume = Resume(
        user_id=user_id,
        original_filename=upload.original_filename,
        storage_path=storage_path,
        content_type=upload.content_type,
        file_size_bytes=upload.size_bytes,
        checksum_sha256=upload.checksum_sha256,
        status=ResumeStatus.UPLOADED,
        is_active=True,
    )
    session.add(resume)
    await session.flush()

    # Filename is not logged: it can carry the candidate's name.
    logger.info(
        "resume_uploaded",
        resume_id=str(resume.id),
        content_type=upload.content_type,
        size_bytes=upload.size_bytes,
    )
    return resume, True


async def get_resume(
    session: AsyncSession, *, resume_id: uuid.UUID, user_id: uuid.UUID
) -> Resume:
    """Fetch one resume scoped to its owner, or raise 404."""
    result = await session.execute(
        select(Resume).where(Resume.id == resume_id, Resume.user_id == user_id)
    )
    resume = result.scalar_one_or_none()
    if resume is None:
        raise NotFoundError(
            message="Resume not found.",
            details=[
                ErrorDetail(
                    code="resume_not_found",
                    field="path.resume_id",
                    message=f"No resume with id {resume_id}.",
                )
            ],
        )
    return resume


async def list_resumes(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    page: int = 1,
    page_size: int = 25,
) -> tuple[list[Resume], int]:
    """Page through a user's resumes, newest first."""
    total = await session.scalar(
        select(func.count()).select_from(Resume).where(Resume.user_id == user_id)
    )

    result = await session.execute(
        select(Resume)
        .where(Resume.user_id == user_id)
        .order_by(Resume.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(result.scalars().all()), int(total or 0)


async def get_active_profile(
    session: AsyncSession, *, resume_id: uuid.UUID
) -> ResumeProfile | None:
    """Current profile for a resume, with its skills eagerly loaded."""
    result = await session.execute(
        select(ResumeProfile)
        .where(
            ResumeProfile.resume_id == resume_id,
            ResumeProfile.is_active.is_(True),
        )
        .options(selectinload(ResumeProfile.skills))
    )
    return result.scalar_one_or_none()


async def require_active_profile(
    session: AsyncSession, *, resume_id: uuid.UUID
) -> ResumeProfile:
    profile = await get_active_profile(session, resume_id=resume_id)
    if profile is None:
        raise NotFoundError(
            message="Resume has not been analyzed yet.",
            details=[
                ErrorDetail(
                    code="profile_not_found",
                    field="path.resume_id",
                    message=(
                        "Run POST /api/v1/resumes/{id}/analyze before "
                        "requesting the profile."
                    ),
                )
            ],
        )
    return profile
