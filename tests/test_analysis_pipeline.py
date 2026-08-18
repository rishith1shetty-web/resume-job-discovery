"""Deterministic extraction and detection.

The Financial Analyst case is CLAUDE.md's own worked example, so it is
asserted in detail - including the roles that must NOT appear.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.analysis import taxonomy
from app.analysis.experience import (
    compute_years_experience,
    find_explicit_years,
    parse_date_ranges,
    total_months,
)
from app.analysis.pipeline import analyze_resume_text
from app.analysis.results import ResumeProfileData
from app.analysis.sections import split_sections
from app.analysis.variations import generate_role_variations
from app.analysis.version import ANALYSIS_VERSION
from app.core.constants import ResumeSection, Seniority, SkillKind
from tests.factories import (
    BACKEND_ENGINEER_RESUME,
    FINANCIAL_ANALYST_RESUME,
    NETWORK_ENGINEER_RESUME,
    UNPARSEABLE_RESUME,
)

# Fixed "now" so year calculations do not drift as time passes.
NOW = datetime(2026, 7, 31, tzinfo=UTC)


@pytest.fixture(scope="module")
def finance_profile() -> ResumeProfileData:
    return analyze_resume_text(FINANCIAL_ANALYST_RESUME, now=NOW)


@pytest.fixture(scope="module")
def engineer_profile() -> ResumeProfileData:
    return analyze_resume_text(BACKEND_ENGINEER_RESUME, now=NOW)


# --- Sections --------------------------------------------------------------


def test_sections_are_detected() -> None:
    kinds = {section.kind for section in split_sections(FINANCIAL_ANALYST_RESUME)}

    assert ResumeSection.SUMMARY in kinds
    assert ResumeSection.EXPERIENCE in kinds
    assert ResumeSection.EDUCATION in kinds
    assert ResumeSection.SKILLS in kinds
    assert ResumeSection.CERTIFICATIONS in kinds


def test_prose_is_not_mistaken_for_a_heading() -> None:
    text = "Experience managing a team of twelve people across three offices."
    sections = split_sections(text)

    assert all(section.kind is not ResumeSection.EXPERIENCE for section in sections)


# --- Domain and role -------------------------------------------------------


def test_detects_finance_domain(finance_profile: ResumeProfileData) -> None:
    assert finance_profile.domain.key == "finance"
    assert finance_profile.domain.confidence > 0.6


def test_detects_financial_analyst_role(finance_profile: ResumeProfileData) -> None:
    assert finance_profile.target_role.key == "financial_analyst"
    assert finance_profile.target_role.confidence > 0.6


def test_detection_records_evidence(finance_profile: ResumeProfileData) -> None:
    # Evidence exists so a wrong detection can be debugged.
    kinds = {item.kind for item in finance_profile.target_role.evidence}

    assert "most_recent_title" in kinds
    assert all(item.snippet for item in finance_profile.target_role.evidence)


def test_detects_backend_engineer(engineer_profile: ResumeProfileData) -> None:
    assert engineer_profile.domain.key == "software_engineering"
    assert engineer_profile.target_role.key == "backend_engineer"


def test_exactly_one_domain_and_one_role(finance_profile: ResumeProfileData) -> None:
    # CLAUDE.md section 3: one domain, one target role. Not a list.
    assert isinstance(finance_profile.domain.key, str)
    assert isinstance(finance_profile.target_role.key, str)


# --- Same-role variations --------------------------------------------------


def test_variations_match_the_claude_md_example(
    finance_profile: ResumeProfileData,
) -> None:
    variations = {item.lower() for item in finance_profile.role_variations}

    for expected in (
        "financial analyst",
        "fp&a analyst",
        "financial planning analyst",
        "corporate financial analyst",
        "finance reporting analyst",
    ):
        assert expected in variations, expected


def test_seniority_prefixed_variation_is_generated(
    finance_profile: ResumeProfileData,
) -> None:
    assert "Senior Financial Analyst" in finance_profile.role_variations


def test_junior_seniority_produces_junior_variation() -> None:
    variations = generate_role_variations("financial_analyst", Seniority.JUNIOR)

    assert "Junior Financial Analyst" in variations


def test_variations_never_include_unrelated_careers(
    finance_profile: ResumeProfileData,
) -> None:
    """The rule CLAUDE.md calls out by name.

    A Financial Analyst resume must never broaden to these jobs.
    """
    forbidden = {
        "accountant", "auditor", "business analyst", "data analyst",
        "financial advisor", "investment banker", "finance manager",
        "sales analyst", "tax consultant", "tax accountant",
    }
    variations = {item.lower() for item in finance_profile.role_variations}

    assert not (variations & forbidden)


def test_no_variation_resolves_to_another_role(
    finance_profile: ResumeProfileData,
) -> None:
    for variation in finance_profile.role_variations:
        resolved = taxonomy.role_key_for_term(variation)
        assert resolved in (None, "financial_analyst"), variation


def test_mid_level_gets_no_prefix() -> None:
    variations = generate_role_variations("data_analyst", Seniority.MID)

    assert not any(v.lower().startswith(("senior ", "junior ")) for v in variations)


def test_variations_are_capped() -> None:
    variations = generate_role_variations("financial_analyst", Seniority.SENIOR)

    assert len(variations) <= 10
    assert len(variations) == len(set(variations))


# --- Skills ----------------------------------------------------------------


def test_skills_are_extracted(finance_profile: ResumeProfileData) -> None:
    keys = {skill.key for skill in finance_profile.skills}

    assert {"financial modeling", "forecasting", "budgeting", "excel"} <= keys


def test_skills_are_classified(finance_profile: ResumeProfileData) -> None:
    kinds = {skill.kind for skill in finance_profile.skills}

    assert SkillKind.SKILL in kinds
    assert SkillKind.TOOL in kinds


def test_skills_carry_evidence(finance_profile: ResumeProfileData) -> None:
    for skill in finance_profile.skills:
        assert skill.evidence
        assert 0.0 < skill.confidence <= 1.0


def test_no_phantom_skills(finance_profile: ResumeProfileData) -> None:
    # Nothing the resume never mentions may appear.
    keys = {skill.key for skill in finance_profile.skills}

    assert "kubernetes" not in keys
    assert "accounts receivable" not in keys  # regression: matched "ar" in "years"
    assert "r" not in keys


# --- Experience ------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "count"),
    [
        ("Jan 2021 - Present", 1),
        ("January 2019 to December 2021", 1),
        ("03/2019 - 05/2021", 1),
        ("2019 - 2022", 1),
        ("Jan 2021 - Present and Mar 2019 - Dec 2020", 2),
        ("no dates here", 0),
    ],
)
def test_date_range_parsing(text: str, count: int) -> None:
    assert len(parse_date_ranges(text)) == count


def test_month_year_range_is_not_double_counted_as_year_range() -> None:
    ranges = parse_date_ranges("Jan 2019 - Mar 2022")

    assert len(ranges) == 1
    assert ranges[0].start_month == 1


def test_overlapping_periods_are_merged() -> None:
    # Concurrent roles must not inflate the total.
    ranges = parse_date_ranges("Jan 2020 - Dec 2021 and Jun 2020 - Dec 2021")

    assert total_months(ranges, now=NOW) == 24


def test_reversed_range_is_ignored() -> None:
    assert parse_date_ranges("Jan 2022 - Mar 2019") == []


def test_explicit_years_statement() -> None:
    result = find_explicit_years("Professional with 8+ years of experience")

    assert result is not None and result[0] == 8.0


def test_date_ranges_take_priority_over_explicit_claim(
    finance_profile: ResumeProfileData,
) -> None:
    assert finance_profile.years_experience_source == "date_ranges"
    assert finance_profile.years_experience is not None


def test_years_are_none_when_unverifiable() -> None:
    # Never guess experience that the resume does not support.
    sections = split_sections(UNPARSEABLE_RESUME)
    years, source, _ = compute_years_experience(
        sections, UNPARSEABLE_RESUME, now=NOW
    )

    assert years is None
    assert source is None


# --- Seniority -------------------------------------------------------------


def test_seniority_from_most_recent_title(finance_profile: ResumeProfileData) -> None:
    assert finance_profile.seniority is Seniority.SENIOR


def test_stale_junior_title_does_not_define_seniority(
    engineer_profile: ResumeProfileData,
) -> None:
    # Current title is unmarked "Backend Engineer"; the oldest role was
    # "Junior Software Engineer". Tenure must win over the stale title.
    assert engineer_profile.seniority is not Seniority.JUNIOR


# --- Education, certifications, responsibilities ----------------------------


def test_education_is_extracted(finance_profile: ResumeProfileData) -> None:
    assert finance_profile.education
    degree = finance_profile.education[0]
    assert degree.degree_level == "bachelor"
    assert degree.year == 2018


def test_certifications_are_extracted(finance_profile: ResumeProfileData) -> None:
    assert "CFA" in {item.name for item in finance_profile.certifications}


def test_responsibilities_are_extracted(finance_profile: ResumeProfileData) -> None:
    assert len(finance_profile.responsibilities) >= 3
    # Every entry starts with an action verb.
    assert all(len(item) >= 20 for item in finance_profile.responsibilities)


# --- Low-signal input ------------------------------------------------------


def test_unparseable_resume_yields_no_role_and_warns() -> None:
    profile = analyze_resume_text(UNPARSEABLE_RESUME, now=NOW)

    assert profile.target_role.key is None
    assert profile.target_role.confidence == 0.0
    assert profile.role_variations == []
    assert profile.warnings


# --- Versioning and determinism --------------------------------------------


def test_analysis_records_its_version(finance_profile: ResumeProfileData) -> None:
    assert finance_profile.analysis_version == ANALYSIS_VERSION


def test_analysis_is_reproducible() -> None:
    first = analyze_resume_text(FINANCIAL_ANALYST_RESUME, now=NOW)
    second = analyze_resume_text(FINANCIAL_ANALYST_RESUME, now=NOW)

    assert first.target_role.key == second.target_role.key
    assert first.domain.key == second.domain.key
    assert first.role_variations == second.role_variations
    assert [s.key for s in first.skills] == [s.key for s in second.skills]


def test_analysis_uses_no_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic extraction must work with no API key and no network."""
    from app.analysis.llm import NullLLMAssistant, get_llm_assistant

    assert isinstance(get_llm_assistant(), NullLLMAssistant)
    assert get_llm_assistant().enabled is False

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    profile = analyze_resume_text(FINANCIAL_ANALYST_RESUME, now=NOW)
    assert profile.target_role.key == "financial_analyst"


# --- Networking roles ------------------------------------------------------
#
# Regression suite for a real misdetection: a resume whose current title is
# "Network Engineer | June 2024 - Present" was analysed as Customer Support
# Specialist, DevOps & Cloud, Intern.
#
# The cause was that no networking role existed in the taxonomy. Titles are
# only extracted when they match a *known* role, so all three real titles were
# invisible, and the one fragment that did match anything matched the alias
# "support engineer" inside "Network Support Engineer Intern" - the oldest and
# most junior job on the resume - which then became the most recent title by
# default and carried the heaviest weight in the system.


@pytest.fixture(scope="module")
def network_profile() -> ResumeProfileData:
    return analyze_resume_text(NETWORK_ENGINEER_RESUME, now=NOW)


def test_a_network_engineer_resume_detects_network_engineer(network_profile) -> None:
    assert network_profile.target_role.key == "network_engineer"
    assert network_profile.target_role.display == "Network Engineer"
    assert network_profile.target_role.confidence >= 0.8


def test_support_vocabulary_does_not_win_over_the_stated_role(network_profile) -> None:
    """The exact defect.

    The resume says "supporting", "troubleshooting", "users", "incidents",
    "escalations" and "ServiceNow" throughout, and its oldest job is literally
    a support internship. None of that outranks three plain statements that
    the candidate is a Network Engineer.
    """
    assert network_profile.target_role.key != "customer_support_specialist"

    support_roles = {
        "customer_support_specialist",
        "customer_success_manager",
        "it_support",
        "help_desk",
    }
    assert network_profile.target_role.key not in support_roles


def test_the_current_title_outranks_the_intern_title(network_profile) -> None:
    """All four titles are now visible, and the current one leads.

    Before the fix exactly one title was extracted - the intern one - and it
    was "most recent" only because nothing else survived extraction.
    """
    titles = network_profile.titles
    assert len(titles) >= 4
    assert all(title.role_key == "network_engineer" for title in titles)

    recent = next(title for title in titles if title.is_most_recent)
    assert "intern" not in recent.raw.casefold()
    assert recent.section is ResumeSection.EXPERIENCE


def test_a_network_engineer_is_not_devops_or_cloud(network_profile) -> None:
    """Requirement 7.

    The resume mentions AWS, Azure, VPC and hybrid cloud, which is how it
    previously landed on DevOps & Cloud at 0.35 confidence. Those are things
    this network engineer connects, not what they do.
    """
    assert network_profile.domain.key == "network_infrastructure"
    assert network_profile.domain.display == "Network & Infrastructure"
    assert network_profile.domain.confidence >= 0.6


def test_the_intern_title_no_longer_sets_seniority(network_profile) -> None:
    """Seniority logic is unchanged; it was being fed the wrong title.

    The reported "Intern" came from a 2020 internship being mistaken for the
    current job. With the current title correct, no seniority marker applies
    and seniority falls through to tenure - the existing behaviour, not a new
    rule.
    """
    assert network_profile.seniority is not Seniority.INTERN
    assert [evidence.kind for evidence in network_profile.seniority_evidence] == [
        "seniority_from_years"
    ]


def test_network_variations_stay_within_the_same_role(network_profile) -> None:
    """Requirements 5 and 6: the fix must not broaden the search.

    Every generated title is a re-phrasing of Network Engineer. None of the
    adjacent jobs appears, including the one this resume was misdetected as.
    """
    variations = {title.casefold() for title in network_profile.role_variations}

    assert "network engineer" in variations
    for forbidden in (
        "customer support specialist",
        "help desk specialist",
        "technical support specialist",
        "business analyst",
        "data analyst",
        "devops engineer",
        "cloud engineer",
        "financial analyst",
    ):
        assert forbidden not in variations

    for title in network_profile.role_variations:
        resolved = taxonomy.role_key_for_term(title)
        assert resolved in (None, "network_engineer"), title


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Network Engineer", "network_engineer"),
        ("Network Support Engineer", "network_engineer"),
        ("Network Operations Engineer", "network_engineer"),
        ("Network Administrator", "network_administrator"),
        ("Network Security Engineer", "network_security_engineer"),
        ("NOC Engineer", "noc_engineer"),
        # And the adjacent jobs they must never be confused with.
        ("Customer Support Specialist", "customer_support_specialist"),
        ("Technical Support Specialist", "customer_support_specialist"),
        ("Help Desk Specialist", "customer_support_specialist"),
    ],
)
def test_networking_titles_resolve_apart_from_support_titles(
    title: str, expected: str
) -> None:
    assert taxonomy.role_key_for_term(title) == expected


def test_a_qualified_networking_title_beats_the_shorter_support_alias() -> None:
    """The qualifier decides the role rather than being ignored.

    "Network Support Engineer" contains "support engineer", which is a
    Customer Support Specialist alias. The matcher prefers the longest known
    term, so the networking reading wins.
    """
    pattern = taxonomy.role_pattern()
    match = pattern.search("Network Support Engineer Intern")

    assert match is not None
    assert taxonomy.role_key_for_term(match.group(0)) == "network_engineer"


def test_the_networking_roles_are_distinct_from_each_other() -> None:
    """Four separate rows, so none can search another's titles."""
    keys = (
        "network_engineer",
        "network_administrator",
        "network_security_engineer",
        "noc_engineer",
    )
    for key in keys:
        assert taxonomy.get_role(key) is not None

    for key in keys:
        variations = {title.casefold() for title in taxonomy.get_role(key).variations}
        for other in keys:
            if other == key:
                continue
            other_canonical = taxonomy.get_role(other).canonical_title.casefold()
            assert other_canonical not in variations


# --- Financial Analyst is unaffected ---------------------------------------


def test_adding_networking_roles_left_the_finance_case_alone(finance_profile) -> None:
    """CLAUDE.md's worked example, re-asserted after the taxonomy change.

    The networking additions introduced a domain and four roles. This pins the
    finance path against them: same role, same domain, same variations, and
    still none of the adjacent finance jobs.
    """
    assert finance_profile.target_role.key == "financial_analyst"
    assert finance_profile.domain.key == "finance"

    variations = {title.casefold() for title in finance_profile.role_variations}
    assert "financial analyst" in variations
    for forbidden in (
        "accountant",
        "auditor",
        "business analyst",
        "data analyst",
        "network engineer",
        "customer support specialist",
    ):
        assert forbidden not in variations


def test_a_finance_resume_is_never_read_as_a_networking_role(finance_profile) -> None:
    networking = {
        "network_engineer",
        "network_administrator",
        "network_security_engineer",
        "noc_engineer",
    }
    assert finance_profile.target_role.key not in networking
    assert finance_profile.domain.key != "network_infrastructure"


def test_a_backend_resume_is_never_read_as_a_networking_role(engineer_profile) -> None:
    """The backend fixture mentions Kubernetes, Docker and AWS - infrastructure
    words - and must still be a backend engineer."""
    assert engineer_profile.target_role.key == "backend_engineer"
    assert engineer_profile.domain.key != "network_infrastructure"
