"""Resume and profile request/response schemas.

Note what is deliberately absent: no endpoint returns the extracted resume
text. It is stored for analysis and re-analysis, not exposed - private resume
content must not leak through the API (Phase 2 rule).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.constants import ResumeStatus, Seniority, SkillKind


class ResumeSummary(BaseModel):
    """A resume as it appears in lists."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    original_filename: str
    content_type: str
    file_size_bytes: int
    checksum_sha256: str
    status: ResumeStatus
    is_active: bool
    created_at: datetime
    updated_at: datetime


class ResumeDetail(ResumeSummary):
    """A single resume, including whether it has been analysed."""

    has_profile: bool = Field(
        default=False, description="True once the resume has been analyzed."
    )
    analysis_version: str | None = Field(
        default=None, description="Version of the logic that produced the profile."
    )


class ResumeUploadResponse(BaseModel):
    """Result of an upload.

    ``created`` is False when byte-identical content was already uploaded; the
    existing resume is returned rather than a duplicate being made.
    """

    resume: ResumeDetail
    created: bool = Field(
        description="False when this upload matched an existing resume."
    )
    duplicate_of: uuid.UUID | None = Field(
        default=None,
        description="Existing resume id when this upload was a duplicate.",
    )


class EvidenceItem(BaseModel):
    """Why the extractor reached a conclusion."""

    kind: str
    value: str
    section: str | None = None
    line_number: int | None = None
    snippet: str = ""
    weight: float = 0.0


class DetectionOut(BaseModel):
    """A detected domain or target role with its supporting evidence."""

    key: str | None
    display: str | None
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    #: Roles that scored exactly the same as the winner. Non-empty means the
    #: evidence did not choose, and the dashboard should say so rather than
    #: render the result like a confident one. The matching human-readable
    #: warning is already in ``warnings``; these fields let the UI point at
    #: the field itself.
    tied_with: list[str] = Field(default_factory=list)
    tie_break: str | None = None


class SkillOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    skill_key: str
    display: str
    kind: SkillKind
    occurrences: int
    confidence: float
    sections: list[str] = Field(default_factory=list)


class EducationOut(BaseModel):
    raw: str
    degree_level: str | None = None
    field_of_study: str | None = None
    institution: str | None = None
    year: int | None = None


class CertificationOut(BaseModel):
    raw: str
    name: str
    year: int | None = None


class TitleOut(BaseModel):
    raw: str
    normalized: str
    role_key: str | None = None
    section: str | None = None
    line_number: int | None = None
    is_most_recent: bool = False


class ResumeProfileOut(BaseModel):
    """The analysed candidate profile.

    Exactly one domain and one target role, per CLAUDE.md section 3. There is
    no list of alternative careers.
    """

    id: uuid.UUID
    resume_id: uuid.UUID
    analysis_version: str
    is_active: bool
    analyzed_at: datetime

    domain: DetectionOut
    target_role: DetectionOut

    confirmed_domain: str | None = None
    confirmed_target_role: str | None = None
    confirmed_at: datetime | None = None
    is_confirmed: bool = False

    # What searching will actually use: the confirmed value when the user has
    # corrected it, otherwise the detected value.
    effective_domain: str | None = None
    effective_target_role: str | None = None

    seniority: Seniority | None = None
    seniority_confidence: float = 0.0
    seniority_evidence: list[EvidenceItem] = Field(default_factory=list)

    years_experience: float | None = Field(
        default=None,
        description="Null when the resume provides nothing verifiable.",
    )
    years_experience_source: str | None = Field(
        default=None, description="'date_ranges' or 'explicit_statement'."
    )

    # Same-role titles only. Never unrelated careers.
    role_variations: list[str] = Field(default_factory=list)

    skills: list[SkillOut] = Field(default_factory=list)
    titles: list[TitleOut] = Field(default_factory=list)
    responsibilities: list[str] = Field(default_factory=list)
    education: list[EducationOut] = Field(default_factory=list)
    certifications: list[CertificationOut] = Field(default_factory=list)

    sections_found: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ProfileUpdateRequest(BaseModel):
    """User correction to the detected domain and/or target role.

    CLAUDE.md section 3 allows reviewing and correcting these before searching.
    Both fields are optional, but at least one must be supplied.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "confirmed_domain": "finance",
                "confirmed_target_role": "financial_analyst",
            }
        }
    )

    confirmed_domain: Annotated[str | None, Field(max_length=64)] = None
    confirmed_target_role: Annotated[str | None, Field(max_length=64)] = None

    @model_validator(mode="after")
    def _require_one(self) -> ProfileUpdateRequest:
        if self.confirmed_domain is None and self.confirmed_target_role is None:
            raise ValueError(
                "Provide confirmed_domain, confirmed_target_role, or both."
            )
        return self


def evidence_from_json(raw: Any) -> list[EvidenceItem]:
    """Build evidence items from the JSONB column, tolerating older shapes."""
    if not isinstance(raw, list):
        return []
    items: list[EvidenceItem] = []
    for entry in raw:
        if isinstance(entry, dict):
            items.append(EvidenceItem(**{**{"kind": "", "value": ""}, **entry}))
    return items


def detection_from_json(
    raw: Any, *, key: str | None, display: str | None, confidence: float
) -> DetectionOut:
    """Rebuild a detection from its stored JSONB payload."""
    evidence: list[EvidenceItem] = []
    tied_with: list[str] = []
    tie_break: str | None = None
    if isinstance(raw, dict):
        evidence = evidence_from_json(raw.get("evidence"))
        stored_tied = raw.get("tied_with")
        if isinstance(stored_tied, list):
            tied_with = [item for item in stored_tied if isinstance(item, str)]
        stored_break = raw.get("tie_break")
        if isinstance(stored_break, str):
            tie_break = stored_break
    return DetectionOut(
        key=key,
        display=display,
        confidence=confidence,
        evidence=evidence,
        tied_with=tied_with,
        tie_break=tie_break,
    )
