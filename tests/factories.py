"""Test data builders.

Resume documents are generated in memory rather than committed as binary
fixtures, so the suite has no opaque test assets and the generators double as
a check that the extractors handle real files.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from io import BytesIO

import docx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import JobUrlKind, ResumeStatus
from app.models.job import Job, JobMatch, JobSource
from app.models.portal import Portal
from app.models.profile import ResumeProfile
from app.models.resume import Resume
from app.models.user import User
from tests.conftest import TEST_RECORD_MARKER, TEST_USER_EMAIL
from tests.pdf_builder import build_blank_pdf, build_pdf_from_text

# Worked example from CLAUDE.md section 3: Finance / Financial Analyst.
FINANCIAL_ANALYST_RESUME = f"""Jane {TEST_RECORD_MARKER} Doe
jane.doe@test.invalid | New York, NY

PROFESSIONAL SUMMARY
Detail-oriented finance professional with 6 years of experience in financial
planning and analysis, budgeting and corporate finance.

WORK EXPERIENCE
Senior Financial Analyst | Acme Corporation | Jan 2021 - Present
- Built financial models and forecasting tools supporting a $40M budget
- Led monthly variance analysis and reporting for three business units
- Developed FP&A dashboards in Power BI used by executive leadership

Financial Analyst | Globex Inc | Mar 2019 - Dec 2020
- Prepared budgeting packages and quarterly financial reporting
- Automated Excel reporting workflows, reducing close time by 30%

EDUCATION
Bachelor of Science in Finance, New York University, 2018

SKILLS
Financial modeling, forecasting, budgeting, variance analysis, FP&A,
Advanced Excel, Power BI, SQL, SAP

CERTIFICATIONS
CFA Level II Candidate
"""

BACKEND_ENGINEER_RESUME = f"""John {TEST_RECORD_MARKER} Smith

SUMMARY
Backend engineer building distributed services.

EXPERIENCE
Backend Engineer | Initech | Feb 2021 - Present
- Designed REST APIs serving 2M requests per day
- Migrated services to Kubernetes and Docker

Junior Software Engineer | Hooli | Jun 2019 - Jan 2021
- Developed Python microservices backed by PostgreSQL

EDUCATION
Bachelor of Science in Computer Science, State University, 2019

SKILLS
Python, FastAPI, PostgreSQL, Docker, Kubernetes, AWS, REST API, Git
"""

# No recognisable role, section or skill - used for the low-signal path.
UNPARSEABLE_RESUME = """Hello there

I like long walks and interesting conversations about nothing in particular.
Please get in touch if that sounds appealing to you as well.
"""


def make_txt(text: str = FINANCIAL_ANALYST_RESUME) -> bytes:
    return text.encode("utf-8")


def make_pdf(text: str = FINANCIAL_ANALYST_RESUME) -> bytes:
    """Build a real PDF with selectable text."""
    return build_pdf_from_text(text)


def make_scanned_pdf() -> bytes:
    """A PDF with pages but no selectable text, like a scan."""
    return build_blank_pdf()


def make_docx(text: str = FINANCIAL_ANALYST_RESUME) -> bytes:
    """Build a real DOCX, including a table to exercise table extraction."""
    document = docx.Document()
    for line in text.split("\n"):
        document.add_paragraph(line)

    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "Tools"
    table.rows[0].cells[1].text = "Tableau"

    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def upload_files(
    data: bytes, filename: str = "resume.pdf", content_type: str = "application/pdf"
) -> dict[str, tuple[str, bytes, str]]:
    """Build the multipart payload for httpx."""
    return {"file": (filename, data, content_type)}


# ---------------------------------------------------------------------------
# Database builders (Phase 3)
#
# Every record carries TEST_RECORD_MARKER so anything that somehow escaped the
# transaction rollback is greppable. Nothing here commits.
# ---------------------------------------------------------------------------


async def make_portal(
    session: AsyncSession,
    *,
    slug: str = "test-portal",
    display_name: str | None = None,
    is_enabled: bool = True,
    is_implemented: bool = False,
) -> Portal:
    portal = Portal(
        slug=f"{TEST_RECORD_MARKER}-{slug}",
        display_name=display_name or f"{TEST_RECORD_MARKER} {slug}",
        is_enabled=is_enabled,
        is_implemented=is_implemented,
        notes=TEST_RECORD_MARKER,
    )
    session.add(portal)
    await session.flush()
    return portal


async def make_user(session: AsyncSession, *, email: str | None = None) -> User:
    user = User(
        email=email or f"{uuid.uuid4().hex[:8]}+{TEST_USER_EMAIL}",
        full_name=f"{TEST_RECORD_MARKER} user",
    )
    session.add(user)
    await session.flush()
    return user


async def make_resume(
    session: AsyncSession, *, user: User, checksum: str | None = None
) -> Resume:
    resume = Resume(
        user_id=user.id,
        original_filename=f"{TEST_RECORD_MARKER}-resume.txt",
        storage_path=f"{TEST_RECORD_MARKER}/resume.txt",
        content_type="text/plain",
        file_size_bytes=len(FINANCIAL_ANALYST_RESUME),
        checksum_sha256=checksum or uuid.uuid4().hex * 2,
        status=ResumeStatus.PARSED,
    )
    session.add(resume)
    await session.flush()
    return resume


async def make_profile(
    session: AsyncSession,
    *,
    resume: Resume,
    domain: str = "finance",
    target_role: str = "financial_analyst",
) -> ResumeProfile:
    profile = ResumeProfile(
        resume_id=resume.id,
        analysis_version="test-1.0.0",
        is_active=True,
        detected_domain=domain,
        detected_domain_display=domain.title(),
        domain_confidence=0.9,
        detected_target_role=target_role,
        detected_target_role_display=target_role.replace("_", " ").title(),
        target_role_confidence=0.9,
    )
    session.add(profile)
    await session.flush()
    return profile


async def make_job(
    session: AsyncSession,
    *,
    title: str = "Financial Analyst",
    company: str = "Acme Corporation",
    canonical_key: str | None = None,
    posted_at: datetime | None = None,
    expired_at: datetime | None = None,
    preferred_application_url: str | None = None,
    description: str | None = None,
    location: str = "New York, NY",
) -> Job:
    job = Job(
        canonical_key=canonical_key or f"{TEST_RECORD_MARKER}:{uuid.uuid4().hex}",
        title=title,
        normalized_title=title.lower(),
        company=company,
        normalized_company=company.lower(),
        location=location,
        description=description,
        posted_at=posted_at,
        expired_at=expired_at,
        preferred_application_url=preferred_application_url,
        preferred_url_kind=(
            JobUrlKind.EMPLOYER_CAREER if preferred_application_url else None
        ),
    )
    session.add(job)
    await session.flush()
    return job


async def make_job_source(
    session: AsyncSession,
    *,
    job: Job,
    portal: Portal,
    external_id: str | None = None,
    application_url: str | None = None,
    url_kind: JobUrlKind = JobUrlKind.PORTAL_LISTING,
    is_preferred: bool = False,
) -> JobSource:
    source = JobSource(
        job_id=job.id,
        portal_id=portal.id,
        external_id=external_id,
        source_url=f"https://example.invalid/{portal.slug}/{uuid.uuid4().hex[:8]}",
        application_url=application_url,
        url_kind=url_kind,
        is_preferred=is_preferred,
    )
    session.add(source)
    await session.flush()
    return source


async def make_match(
    session: AsyncSession,
    *,
    resume: Resume,
    job: Job,
    search_run_id: uuid.UUID | None = None,
    final_score: float = 85.0,
    ranking_score: float | None = None,
    qualified: bool = True,
    matched_at: datetime | None = None,
) -> JobMatch:
    """Score a job for a resume.

    Pass ``matched_at`` when a test depends on which of several scores is the
    newest: rows written microseconds apart can otherwise share a timestamp.
    """
    match = JobMatch(
        resume_id=resume.id,
        job_id=job.id,
        search_run_id=search_run_id,
        final_score=final_score,
        role_score=final_score,
        skill_score=final_score,
        ranking_score=ranking_score if ranking_score is not None else final_score,
        qualified=qualified,
        scoring_version="test-0",
        **({"matched_at": matched_at} if matched_at is not None else {}),
    )
    session.add(match)
    await session.flush()
    return match
