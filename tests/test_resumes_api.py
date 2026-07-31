"""Integration tests for the resume endpoints.

Every test runs inside a transaction that is rolled back, so nothing is
committed to resume_job_discovery. Records carry the pytest marker as a second
line of defence. Nothing here truncates, drops or resets anything.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ResumeStatus
from app.models.profile import ResumeProfile, ResumeText
from app.models.resume import Resume
from tests.conftest import REQUIRED_DATABASE, TEST_RECORD_MARKER
from tests.factories import (
    BACKEND_ENGINEER_RESUME,
    UNPARSEABLE_RESUME,
    make_docx,
    make_pdf,
    make_txt,
    upload_files,
)

BASE = "/api/v1/resumes"
DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


async def _upload(
    client: AsyncClient,
    data: bytes | None = None,
    filename: str = "resume.txt",
    content_type: str = "text/plain",
) -> dict[str, Any]:
    response = await client.post(
        BASE, files=upload_files(data if data is not None else make_txt(), filename, content_type)
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def _upload_and_analyze(client: AsyncClient) -> tuple[str, dict[str, Any]]:
    uploaded = await _upload(client)
    resume_id = uploaded["resume"]["id"]

    response = await client.post(f"{BASE}/{resume_id}/analyze")
    assert response.status_code == 200, response.text
    return resume_id, response.json()["data"]


# --- Safety ----------------------------------------------------------------


async def test_tests_target_the_correct_database(db_session: AsyncSession) -> None:
    name = await db_session.scalar(text("SELECT current_database()"))

    assert name == REQUIRED_DATABASE
    assert name != "linkedin_auto_apply"


async def test_writes_are_rolled_back(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Rows exist inside the test and vanish when the transaction rolls back.

    Visible here only as "the row is present now"; the rollback itself is what
    the fixture guarantees, and the next test's empty state confirms it.
    """
    await _upload(api_client)

    count = await db_session.scalar(
        select(Resume).where(Resume.checksum_sha256.isnot(None)).exists().select()
    )
    assert count is True


async def test_previous_test_data_did_not_persist(db_session: AsyncSession) -> None:
    # Runs after the upload test above; rollback means nothing survived.
    result = await db_session.execute(
        select(Resume).where(Resume.original_filename.like(f"%{TEST_RECORD_MARKER}%"))
    )
    assert result.scalars().all() == []


# --- Upload ----------------------------------------------------------------


async def test_upload_txt(api_client: AsyncClient) -> None:
    data = await _upload(api_client)

    assert data["created"] is True
    assert data["resume"]["status"] == ResumeStatus.UPLOADED.value
    assert data["resume"]["content_type"] == "text/plain"
    assert len(data["resume"]["checksum_sha256"]) == 64


async def test_upload_pdf(api_client: AsyncClient) -> None:
    data = await _upload(api_client, make_pdf(), "resume.pdf", "application/pdf")

    assert data["resume"]["content_type"] == "application/pdf"


async def test_upload_docx(api_client: AsyncClient) -> None:
    data = await _upload(api_client, make_docx(), "resume.docx", DOCX_TYPE)

    assert data["resume"]["content_type"] == DOCX_TYPE


async def test_upload_uses_the_envelope(api_client: AsyncClient) -> None:
    response = await api_client.post(BASE, files=upload_files(make_txt(), "r.txt", "text/plain"))
    body = response.json()

    assert body["success"] is True
    assert body["errors"] == []
    assert "data" in body


async def test_duplicate_upload_returns_existing_resume(
    api_client: AsyncClient,
) -> None:
    first = await _upload(api_client)
    second = await _upload(api_client)

    assert first["created"] is True
    assert second["created"] is False
    assert second["resume"]["id"] == first["resume"]["id"]
    assert second["duplicate_of"] == first["resume"]["id"]


async def test_upload_rejects_oversized_file(api_client: AsyncClient) -> None:
    response = await api_client.post(
        BASE, files=upload_files(b"x" * (11 * 1024 * 1024), "big.txt", "text/plain")
    )

    assert response.status_code == 413
    assert response.json()["success"] is False


async def test_upload_rejects_unsupported_type(api_client: AsyncClient) -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    response = await api_client.post(BASE, files=upload_files(png, "x.png", "image/png"))

    assert response.status_code == 415
    assert response.json()["errors"][0]["code"] == "unrecognised_file_content"


async def test_upload_rejects_empty_file(api_client: AsyncClient) -> None:
    response = await api_client.post(BASE, files=upload_files(b"", "x.txt", "text/plain"))

    assert response.status_code == 422


async def test_upload_sanitizes_traversal_filename(api_client: AsyncClient) -> None:
    data = await _upload(api_client, make_txt(), "../../../etc/passwd.txt", "text/plain")

    assert "/" not in data["resume"]["original_filename"]
    assert ".." not in data["resume"]["original_filename"]


# --- List and get ----------------------------------------------------------


async def test_list_is_paginated(api_client: AsyncClient) -> None:
    await _upload(api_client)

    response = await api_client.get(BASE)
    body = response.json()

    assert response.status_code == 200
    assert body["data"]["pagination"]["total"] >= 1
    assert set(body["data"]["pagination"]) == {
        "page", "page_size", "total", "total_pages", "has_next", "has_prev",
    }


async def test_list_respects_page_size(api_client: AsyncClient) -> None:
    await _upload(api_client)
    await _upload(api_client, BACKEND_ENGINEER_RESUME.encode(), "b.txt", "text/plain")

    body = (await api_client.get(f"{BASE}?page=1&page_size=1")).json()

    assert len(body["data"]["items"]) == 1


async def test_list_rejects_invalid_pagination(api_client: AsyncClient) -> None:
    response = await api_client.get(f"{BASE}?page=0")

    assert response.status_code == 422
    assert response.json()["errors"][0]["field"] == "query.page"


async def test_get_one_resume(api_client: AsyncClient) -> None:
    uploaded = await _upload(api_client)
    resume_id = uploaded["resume"]["id"]

    body = (await api_client.get(f"{BASE}/{resume_id}")).json()

    assert body["data"]["id"] == resume_id
    assert body["data"]["has_profile"] is False


async def test_get_missing_resume_returns_404(api_client: AsyncClient) -> None:
    response = await api_client.get(f"{BASE}/00000000-0000-0000-0000-000000000000")

    assert response.status_code == 404
    assert response.json()["errors"][0]["code"] == "resume_not_found"


# --- Analyze ---------------------------------------------------------------


async def test_analyze_detects_domain_and_role(api_client: AsyncClient) -> None:
    _, profile = await _upload_and_analyze(api_client)

    assert profile["domain"]["key"] == "finance"
    assert profile["target_role"]["key"] == "financial_analyst"
    assert profile["domain"]["confidence"] > 0.6
    assert profile["target_role"]["confidence"] > 0.6


async def test_analyze_returns_evidence(api_client: AsyncClient) -> None:
    _, profile = await _upload_and_analyze(api_client)

    assert profile["target_role"]["evidence"]
    assert all("snippet" in item for item in profile["target_role"]["evidence"])


async def test_analyze_returns_same_role_variations_only(
    api_client: AsyncClient,
) -> None:
    _, profile = await _upload_and_analyze(api_client)
    variations = {v.lower() for v in profile["role_variations"]}

    assert "financial analyst" in variations
    assert "fp&a analyst" in variations
    forbidden = {"accountant", "auditor", "data analyst", "business analyst"}
    assert not (variations & forbidden)


async def test_analyze_extracts_structured_fields(api_client: AsyncClient) -> None:
    _, profile = await _upload_and_analyze(api_client)

    assert profile["skills"]
    assert profile["education"]
    assert profile["certifications"]
    assert profile["responsibilities"]
    assert profile["seniority"] == "senior"
    assert profile["years_experience"] is not None
    assert profile["analysis_version"]


async def test_analyze_marks_resume_parsed(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    resume_id, _ = await _upload_and_analyze(api_client)

    resume = await db_session.get(Resume, __import__("uuid").UUID(resume_id))
    assert resume is not None
    assert resume.status is ResumeStatus.PARSED


async def test_analyze_stores_text_separately(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    # Requirement: the resume record and the extracted text are separate rows.
    resume_id, _ = await _upload_and_analyze(api_client)

    result = await db_session.execute(
        select(ResumeText).where(
            ResumeText.resume_id == __import__("uuid").UUID(resume_id)
        )
    )
    stored = result.scalar_one()
    assert stored.char_count > 100
    assert stored.extractor == "text-decode"


async def test_reanalysis_creates_a_new_version(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Old analyses stay traceable rather than being overwritten."""
    resume_id, first = await _upload_and_analyze(api_client)

    second_response = await api_client.post(f"{BASE}/{resume_id}/analyze")
    second = second_response.json()["data"]

    assert second["id"] != first["id"]

    result = await db_session.execute(
        select(ResumeProfile).where(
            ResumeProfile.resume_id == __import__("uuid").UUID(resume_id)
        )
    )
    profiles = result.scalars().all()

    assert len(profiles) == 2
    # Exactly one active, enforced by the partial unique index.
    assert sum(1 for p in profiles if p.is_active) == 1


async def test_analyze_missing_resume_returns_404(api_client: AsyncClient) -> None:
    response = await api_client.post(
        f"{BASE}/00000000-0000-0000-0000-000000000000/analyze"
    )

    assert response.status_code == 404


async def test_analyze_low_signal_resume_warns(api_client: AsyncClient) -> None:
    uploaded = await _upload(
        api_client, UNPARSEABLE_RESUME.encode(), "vague.txt", "text/plain"
    )
    resume_id = uploaded["resume"]["id"]

    body = (await api_client.post(f"{BASE}/{resume_id}/analyze")).json()

    assert body["data"]["target_role"]["key"] is None
    assert body["data"]["role_variations"] == []
    assert body["message"]


# --- Profile ---------------------------------------------------------------


async def test_get_profile(api_client: AsyncClient) -> None:
    resume_id, _ = await _upload_and_analyze(api_client)

    body = (await api_client.get(f"{BASE}/{resume_id}/profile")).json()

    assert body["data"]["effective_target_role"] == "financial_analyst"
    assert body["data"]["is_active"] is True


async def test_get_profile_before_analysis_returns_404(
    api_client: AsyncClient,
) -> None:
    uploaded = await _upload(api_client)
    resume_id = uploaded["resume"]["id"]

    response = await api_client.get(f"{BASE}/{resume_id}/profile")

    assert response.status_code == 404
    assert response.json()["errors"][0]["code"] == "profile_not_found"


async def test_profile_never_exposes_resume_text(api_client: AsyncClient) -> None:
    # Private content must not leak through the API.
    resume_id, _ = await _upload_and_analyze(api_client)
    response = await api_client.get(f"{BASE}/{resume_id}/profile")

    assert "jane.doe@test.invalid" not in response.text
    assert "content" not in response.json()["data"]


# --- Profile correction ----------------------------------------------------


async def test_confirm_role_updates_variations(api_client: AsyncClient) -> None:
    resume_id, _ = await _upload_and_analyze(api_client)

    response = await api_client.patch(
        f"{BASE}/{resume_id}/profile",
        json={"confirmed_target_role": "investment_analyst"},
    )
    data = response.json()["data"]

    assert response.status_code == 200
    assert data["effective_target_role"] == "investment_analyst"
    assert data["is_confirmed"] is True
    # Variations follow the corrected role.
    assert any("Investment" in v for v in data["role_variations"])


async def test_correction_preserves_the_original_detection(
    api_client: AsyncClient,
) -> None:
    resume_id, _ = await _upload_and_analyze(api_client)

    await api_client.patch(
        f"{BASE}/{resume_id}/profile",
        json={"confirmed_target_role": "investment_analyst"},
    )
    data = (await api_client.get(f"{BASE}/{resume_id}/profile")).json()["data"]

    # The detected value and its evidence remain available for debugging.
    assert data["target_role"]["key"] == "financial_analyst"
    assert data["target_role"]["evidence"]
    assert data["confirmed_target_role"] == "investment_analyst"


async def test_confirm_domain_only(api_client: AsyncClient) -> None:
    resume_id, _ = await _upload_and_analyze(api_client)

    response = await api_client.patch(
        f"{BASE}/{resume_id}/profile", json={"confirmed_domain": "accounting"}
    )

    assert response.status_code == 200
    assert response.json()["data"]["effective_domain"] == "accounting"


async def test_unknown_role_is_rejected(api_client: AsyncClient) -> None:
    resume_id, _ = await _upload_and_analyze(api_client)

    response = await api_client.patch(
        f"{BASE}/{resume_id}/profile",
        json={"confirmed_target_role": "wizard"},
    )

    assert response.status_code == 422
    assert response.json()["errors"][0]["code"] == "unknown_target_role"


async def test_empty_correction_is_rejected(api_client: AsyncClient) -> None:
    resume_id, _ = await _upload_and_analyze(api_client)

    response = await api_client.patch(f"{BASE}/{resume_id}/profile", json={})

    assert response.status_code == 422


async def test_patch_before_analysis_returns_404(api_client: AsyncClient) -> None:
    uploaded = await _upload(api_client)
    resume_id = uploaded["resume"]["id"]

    response = await api_client.patch(
        f"{BASE}/{resume_id}/profile", json={"confirmed_domain": "finance"}
    )

    assert response.status_code == 404


# --- No auto-apply ---------------------------------------------------------


@pytest.mark.parametrize(
    "forbidden",
    ["easy_apply", "apply_priority", "auto_submit", "unsupported_form", "form_progress"],
)
async def test_no_auto_apply_fields_in_responses(
    api_client: AsyncClient, forbidden: str
) -> None:
    resume_id, _ = await _upload_and_analyze(api_client)
    response = await api_client.get(f"{BASE}/{resume_id}/profile")

    assert forbidden not in response.text.lower()
