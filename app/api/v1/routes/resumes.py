"""Resume upload, listing, analysis and profile endpoints."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, File, Query, UploadFile, status

from app.api.deps import AppSettings, CurrentUser, DbSession, Storage
from app.core.constants import ResumeStatus
from app.models.profile import ResumeProfile
from app.models.resume import Resume
from app.schemas.common import Page, PaginationMeta, ResponseEnvelope
from app.schemas.resume import (
    CertificationOut,
    EducationOut,
    ProfileUpdateRequest,
    ResumeDetail,
    ResumeProfileOut,
    ResumeSummary,
    ResumeUploadResponse,
    SkillOut,
    TitleOut,
    detection_from_json,
    evidence_from_json,
)
from app.services import analysis_service, resume_service
from app.services.file_validation import validate_resume_upload

router = APIRouter(prefix="/resumes", tags=["Resumes"])


def _resume_detail(
    resume: Resume, profile: ResumeProfile | None = None
) -> ResumeDetail:
    return ResumeDetail(
        **ResumeSummary.model_validate(resume).model_dump(),
        has_profile=profile is not None,
        analysis_version=profile.analysis_version if profile else None,
    )


def _profile_out(profile: ResumeProfile) -> ResumeProfileOut:
    return ResumeProfileOut(
        id=profile.id,
        resume_id=profile.resume_id,
        analysis_version=profile.analysis_version,
        is_active=profile.is_active,
        analyzed_at=profile.analyzed_at,
        domain=detection_from_json(
            profile.domain_evidence,
            key=profile.detected_domain,
            display=profile.detected_domain_display,
            confidence=profile.domain_confidence,
        ),
        target_role=detection_from_json(
            profile.target_role_evidence,
            key=profile.detected_target_role,
            display=profile.detected_target_role_display,
            confidence=profile.target_role_confidence,
        ),
        confirmed_domain=profile.confirmed_domain,
        confirmed_target_role=profile.confirmed_target_role,
        confirmed_at=profile.confirmed_at,
        is_confirmed=profile.is_confirmed,
        effective_domain=profile.effective_domain,
        effective_target_role=profile.effective_target_role,
        seniority=profile.seniority,
        seniority_confidence=profile.seniority_confidence,
        seniority_evidence=evidence_from_json(profile.seniority_evidence),
        years_experience=profile.years_experience,
        years_experience_source=profile.years_experience_source,
        role_variations=list(profile.role_variations or []),
        skills=[SkillOut.model_validate(skill) for skill in profile.skills],
        titles=[TitleOut(**item) for item in (profile.titles or [])],
        responsibilities=list(profile.responsibilities or []),
        education=[EducationOut(**item) for item in (profile.education or [])],
        certifications=[
            CertificationOut(**item) for item in (profile.certifications or [])
        ],
        sections_found=list(profile.sections_found or []),
        warnings=list(profile.warnings or []),
    )


@router.post(
    "",
    response_model=ResponseEnvelope[ResumeUploadResponse],
    status_code=status.HTTP_201_CREATED,
    summary="Upload a resume",
    description=(
        "Accepts PDF, DOCX or TXT. The file is validated by magic bytes, not "
        "just its name, sanitised, checksummed and stored unmodified.\n\n"
        "Re-uploading byte-identical content returns the existing resume with "
        "`created=false` instead of creating a duplicate."
    ),
    responses={
        413: {"description": "File exceeds the configured size limit."},
        415: {"description": "Unsupported or mismatched file type."},
        422: {"description": "Empty or unreadable file."},
    },
)
async def upload_resume(
    session: DbSession,
    settings: AppSettings,
    storage: Storage,
    user: CurrentUser,
    file: Annotated[UploadFile, File(description="PDF, DOCX or TXT resume.")],
) -> ResponseEnvelope[ResumeUploadResponse]:
    # Read once; the size guard runs inside the validator against real bytes.
    data = await file.read()

    upload = validate_resume_upload(
        filename=file.filename,
        content_type=file.content_type,
        data=data,
        max_upload_mb=settings.max_upload_mb,
    )

    resume, created = await resume_service.create_resume(
        session, storage, user_id=user.id, upload=upload
    )
    await session.commit()

    profile = await resume_service.get_active_profile(session, resume_id=resume.id)

    return ResponseEnvelope[ResumeUploadResponse](
        data=ResumeUploadResponse(
            resume=_resume_detail(resume, profile),
            created=created,
            duplicate_of=None if created else resume.id,
        ),
        message=None if created else "This resume was already uploaded.",
    )


@router.get(
    "",
    response_model=ResponseEnvelope[Page[ResumeSummary]],
    summary="List resumes",
    description="Newest first. Paginated.",
)
async def list_resumes(
    session: DbSession,
    user: CurrentUser,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 25,
) -> ResponseEnvelope[Page[ResumeSummary]]:
    resumes, total = await resume_service.list_resumes(
        session, user_id=user.id, page=page, page_size=page_size
    )

    return ResponseEnvelope[Page[ResumeSummary]](
        data=Page[ResumeSummary](
            items=[ResumeSummary.model_validate(item) for item in resumes],
            pagination=PaginationMeta.build(
                page=page, page_size=page_size, total=total
            ),
        )
    )


@router.get(
    "/{resume_id}",
    response_model=ResponseEnvelope[ResumeDetail],
    summary="Get one resume",
    responses={404: {"description": "Resume not found."}},
)
async def get_resume(
    resume_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
) -> ResponseEnvelope[ResumeDetail]:
    resume = await resume_service.get_resume(
        session, resume_id=resume_id, user_id=user.id
    )
    profile = await resume_service.get_active_profile(session, resume_id=resume.id)
    return ResponseEnvelope[ResumeDetail](data=_resume_detail(resume, profile))


@router.post(
    "/{resume_id}/analyze",
    response_model=ResponseEnvelope[ResumeProfileOut],
    summary="Analyze a resume",
    description=(
        "Runs deterministic extraction and detects exactly one primary domain "
        "and one target role, with confidence scores and evidence.\n\n"
        "Re-analysing creates a new profile version; the previous version is "
        "deactivated but retained so earlier detections stay traceable. No "
        "LLM is involved."
    ),
    responses={
        404: {"description": "Resume not found."},
        422: {"description": "Text could not be extracted (e.g. scanned PDF)."},
    },
)
async def analyze_resume(
    resume_id: uuid.UUID,
    session: DbSession,
    storage: Storage,
    user: CurrentUser,
) -> ResponseEnvelope[ResumeProfileOut]:
    resume = await resume_service.get_resume(
        session, resume_id=resume_id, user_id=user.id
    )

    profile = await analysis_service.analyze_resume(session, storage, resume)
    await session.commit()

    message = None
    if profile.detected_target_role is None:
        message = (
            "No target role could be determined. Set it manually before "
            "searching."
        )

    return ResponseEnvelope[ResumeProfileOut](
        data=_profile_out(profile), message=message
    )


@router.get(
    "/{resume_id}/profile",
    response_model=ResponseEnvelope[ResumeProfileOut],
    summary="Get the analyzed profile",
    responses={404: {"description": "Resume not found, or not yet analyzed."}},
)
async def get_profile(
    resume_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
) -> ResponseEnvelope[ResumeProfileOut]:
    resume = await resume_service.get_resume(
        session, resume_id=resume_id, user_id=user.id
    )
    profile = await resume_service.require_active_profile(
        session, resume_id=resume.id
    )
    return ResponseEnvelope[ResumeProfileOut](data=_profile_out(profile))


@router.patch(
    "/{resume_id}/profile",
    response_model=ResponseEnvelope[ResumeProfileOut],
    summary="Confirm or correct the domain and target role",
    description=(
        "Stores a user correction alongside the detected values rather than "
        "over them, so the original evidence remains available.\n\n"
        "Changing the target role regenerates the same-role search "
        "variations. Unrelated roles are never suggested."
    ),
    responses={
        404: {"description": "Resume not found, or not yet analyzed."},
        422: {"description": "Unknown domain or role key."},
    },
)
async def update_profile(
    resume_id: uuid.UUID,
    payload: ProfileUpdateRequest,
    session: DbSession,
    user: CurrentUser,
) -> ResponseEnvelope[ResumeProfileOut]:
    resume = await resume_service.get_resume(
        session, resume_id=resume_id, user_id=user.id
    )
    profile = await resume_service.require_active_profile(
        session, resume_id=resume.id
    )

    updated = await analysis_service.update_confirmed_profile(
        session,
        profile,
        domain=payload.confirmed_domain,
        target_role=payload.confirmed_target_role,
    )
    await session.commit()

    return ResponseEnvelope[ResumeProfileOut](
        data=_profile_out(updated), message="Profile updated."
    )


__all__ = ["ResumeStatus", "router"]
