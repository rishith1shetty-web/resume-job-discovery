"""Resume analysis orchestration.

Bridges the pure analysis pipeline and the database: extract text once, run
deterministic analysis, then persist a new *versioned* profile.

Re-analysis never overwrites. The previous profile is deactivated and kept, so
an earlier detection and its evidence stay available after the logic changes
(CLAUDE.md section 7).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.analysis import taxonomy
from app.analysis.extraction import ExtractionError, extract_text
from app.analysis.pipeline import analyze_resume_text
from app.analysis.results import ResumeProfileData
from app.analysis.variations import generate_role_variations
from app.core.constants import ResumeStatus, Seniority
from app.core.exceptions import ErrorDetail, UnprocessableError
from app.core.logging import get_logger
from app.models.profile import ResumeProfile, ResumeSkill, ResumeText
from app.models.resume import Resume
from app.services.storage import ResumeStorage

logger = get_logger(__name__)


async def get_or_create_text(
    session: AsyncSession, storage: ResumeStorage, resume: Resume
) -> ResumeText:
    """Extract text from the stored file, reusing it if already extracted.

    Reads the stored copy; the original file is never modified.
    """
    result = await session.execute(
        select(ResumeText).where(ResumeText.resume_id == resume.id)
    )
    existing = result.scalar_one_or_none()
    if existing is not None:
        return existing

    if not storage.exists(resume.storage_path):
        raise UnprocessableError(
            message="Stored resume file is missing.",
            details=[
                ErrorDetail(
                    code="resume_file_missing",
                    message="The uploaded file could not be found on disk.",
                )
            ],
        )

    data = storage.read(resume.storage_path)

    try:
        extracted = extract_text(data, content_type=resume.content_type)
    except ExtractionError as exc:
        resume.status = ResumeStatus.FAILED
        await session.flush()
        # exc.message is written to never contain document content.
        raise UnprocessableError(
            message=exc.message,
            details=[ErrorDetail(code="extraction_failed", message=exc.message)],
        ) from exc

    text = ResumeText(
        resume_id=resume.id,
        content=extracted.text,
        char_count=extracted.char_count,
        page_count=extracted.page_count,
        extractor=extracted.extractor,
        extractor_version=extracted.extractor_version,
        warnings=list(extracted.warnings),
    )
    session.add(text)
    await session.flush()

    # Character count only - never the text itself.
    logger.info(
        "resume_text_extracted",
        resume_id=str(resume.id),
        extractor=extracted.extractor,
        char_count=extracted.char_count,
    )
    return text


def _profile_from_data(
    resume_id: uuid.UUID, data: ResumeProfileData
) -> ResumeProfile:
    return ResumeProfile(
        resume_id=resume_id,
        analysis_version=data.analysis_version,
        is_active=True,
        analyzed_at=datetime.now(UTC),
        detected_domain=data.domain.key,
        detected_domain_display=data.domain.display,
        domain_confidence=data.domain.confidence,
        domain_evidence=data.domain.as_dict(),
        detected_target_role=data.target_role.key,
        detected_target_role_display=data.target_role.display,
        target_role_confidence=data.target_role.confidence,
        target_role_evidence=data.target_role.as_dict(),
        seniority=data.seniority,
        seniority_confidence=data.seniority_confidence,
        seniority_evidence=[item.as_dict() for item in data.seniority_evidence],
        years_experience=data.years_experience,
        years_experience_source=data.years_experience_source,
        years_experience_evidence=[
            item.as_dict() for item in data.years_experience_evidence
        ],
        role_variations=list(data.role_variations),
        titles=[
            {
                "raw": title.raw,
                "normalized": title.normalized,
                "role_key": title.role_key,
                "section": title.section.value,
                "line_number": title.line_number,
                "is_most_recent": title.is_most_recent,
            }
            for title in data.titles
        ],
        responsibilities=list(data.responsibilities),
        education=[
            {
                "raw": item.raw,
                "degree_level": item.degree_level,
                "field_of_study": item.field_of_study,
                "institution": item.institution,
                "year": item.year,
            }
            for item in data.education
        ],
        certifications=[
            {"raw": item.raw, "name": item.name, "year": item.year}
            for item in data.certifications
        ],
        sections_found=[section.value for section in data.sections_found],
        warnings=list(data.warnings),
    )


async def analyze_resume(
    session: AsyncSession,
    storage: ResumeStorage,
    resume: Resume,
) -> ResumeProfile:
    """Analyse a resume and persist a new active profile version."""
    text = await get_or_create_text(session, storage, resume)
    data = analyze_resume_text(text.content)

    # Supersede the previous profile rather than deleting it. Done before the
    # insert so the partial unique index on (resume_id) where is_active never
    # sees two active rows.
    await session.execute(
        update(ResumeProfile)
        .where(
            ResumeProfile.resume_id == resume.id,
            ResumeProfile.is_active.is_(True),
        )
        .values(is_active=False)
    )
    await session.flush()

    profile = _profile_from_data(resume.id, data)
    session.add(profile)
    await session.flush()

    for skill in data.skills:
        session.add(
            ResumeSkill(
                profile_id=profile.id,
                skill_key=skill.key,
                display=skill.display,
                kind=skill.kind,
                occurrences=skill.occurrences,
                confidence=skill.confidence,
                sections=sorted(section.value for section in skill.sections),
                evidence=[item.as_dict() for item in skill.evidence],
            )
        )

    resume.status = ResumeStatus.PARSED
    await session.flush()

    logger.info(
        "resume_analyzed",
        resume_id=str(resume.id),
        profile_id=str(profile.id),
        analysis_version=data.analysis_version,
        domain=data.domain.key,
        target_role=data.target_role.key,
        skill_count=len(data.skills),
    )

    await session.refresh(profile, attribute_names=["skills"])
    return profile


async def update_confirmed_profile(
    session: AsyncSession,
    profile: ResumeProfile,
    *,
    domain: str | None = None,
    target_role: str | None = None,
) -> ResumeProfile:
    """Apply a user correction to the detected domain and target role.

    Corrections are stored alongside the detection, never over it, so the
    original evidence remains available. Changing the role regenerates the
    same-role search variations for the new role.
    """
    errors: list[ErrorDetail] = []

    if domain is not None and taxonomy.get_domain(domain) is None:
        errors.append(
            ErrorDetail(
                code="unknown_domain",
                field="body.confirmed_domain",
                message=f"{domain!r} is not a known domain.",
            )
        )

    if target_role is not None and taxonomy.get_role(target_role) is None:
        errors.append(
            ErrorDetail(
                code="unknown_target_role",
                field="body.confirmed_target_role",
                message=f"{target_role!r} is not a known role.",
            )
        )

    if errors:
        raise UnprocessableError(message="Invalid profile correction.", details=errors)

    if domain is not None:
        profile.confirmed_domain = domain
    if target_role is not None:
        profile.confirmed_target_role = target_role
        # Variations must always describe the role actually being searched.
        seniority: Seniority | None = profile.seniority
        profile.role_variations = generate_role_variations(target_role, seniority)

    profile.confirmed_at = datetime.now(UTC)
    await session.flush()

    logger.info(
        "resume_profile_confirmed",
        profile_id=str(profile.id),
        domain=profile.effective_domain,
        target_role=profile.effective_target_role,
    )

    await session.refresh(profile, attribute_names=["skills"])
    return profile


async def get_profile_with_skills(
    session: AsyncSession, profile_id: uuid.UUID
) -> ResumeProfile | None:
    result = await session.execute(
        select(ResumeProfile)
        .where(ResumeProfile.id == profile_id)
        .options(selectinload(ResumeProfile.skills))
    )
    return result.scalar_one_or_none()
