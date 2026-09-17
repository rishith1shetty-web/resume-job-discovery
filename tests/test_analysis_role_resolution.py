"""How a target role is chosen when the evidence is thin, and what is searched.

Three things are pinned here, because each was a real defect:

1. A resume whose job titles are not in the taxonomy has NO title extracted -
   not a weak one - so the choice falls to core-skill coverage, which ties
   easily. The tie is now broken by the resume's own domain and, either way,
   recorded so the caller can say the result needs review.
2. HR analytics is a career of its own. Naming it keeps its titles out of Data
   Analyst and Business Operations Analyst, which both used to accept them.
3. The queries a portal receives are capped, so the ORDER of the generated
   list decides what survives. A seniority form is a re-phrasing of the
   canonical title and must not be truncated away by the long tail.
"""

from __future__ import annotations

import pytest

from app.analysis import taxonomy
from app.analysis.detection import detect_target_role
from app.analysis.pipeline import analyze_resume_text
from app.analysis.results import ExtractedSkill
from app.analysis.text import normalize_title
from app.analysis.variations import (
    DEFAULT_MAX_VARIATIONS,
    generate_role_variations,
    variations_are_same_role,
)
from app.core.constants import Seniority, SkillKind
from app.matching.profile import ResumeMatchProfile
from app.matching.requirements import extract_requirements
from app.matching.roles import RoleRejection, evaluate_role
from app.portals.adapters.lever import title_matches
from app.search.queries import DEFAULT_MAX_QUERIES_PER_PORTAL, build_query_plan
from tests.factories import (
    FINANCIAL_ANALYST_RESUME,
    HR_ANALYST_RESUME,
    NETWORK_ENGINEER_RESUME,
)

# Long enough to be scoreable, and deliberately generic so that nothing but
# the title can decide the role gate's verdict.
JOB_BODY = (
    "Responsibilities include analysis, reporting and partnering with "
    "stakeholders to deliver insight. Requirements: 3+ years of relevant "
    "experience, a bachelor degree, strong Excel and SQL, and clear "
    "written communication with cross-functional teams. "
) * 4


def _skills(*keys: str) -> list[ExtractedSkill]:
    return [
        ExtractedSkill(key=key, display=key, kind=SkillKind.SKILL, confidence=1.0)
        for key in keys
    ]


def _profile(role_key: str, seniority: Seniority = Seniority.MID) -> ResumeMatchProfile:
    """The candidate side, assembled the way ``build_match_profile`` does."""
    spec = taxonomy.get_role(role_key)
    assert spec is not None
    keys = {normalize_title(v) for v in generate_role_variations(role_key, seniority)}
    keys.update(normalize_title(v) for v in spec.variations)
    keys.update(spec.aliases)
    keys.add(normalize_title(spec.canonical_title))
    return ResumeMatchProfile(
        resume_id="test",
        target_role_key=role_key,
        target_role_display=spec.canonical_title,
        domain_key=spec.domain_key,
        seniority=seniority,
        years_experience=5.0,
        skill_keys=frozenset(spec.core_skills),
        skill_display={key: key for key in spec.core_skills},
        role_variation_keys=frozenset(keys),
        education=("bachelor of science",),
        role_is_confirmed=True,
    )


def _verdict(role_key: str, title: str) -> str:
    assessment = evaluate_role(
        _profile(role_key), extract_requirements(title=title, description=JOB_BODY)
    )
    return assessment.verdict.value if assessment.passed else "REJECTED"


# --- The taxonomy gap ------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "HR Analyst",
        "Human Resources Analyst",
        "HRIS Analyst",
        "HR Data Analyst",
        "People Analytics Analyst",
        "HR Operations Analyst",
        "Workforce Analytics Analyst",
    ],
)
def test_hr_analytics_titles_resolve_to_their_own_role(title: str) -> None:
    """Each of these used to resolve to nothing, so no title was extracted."""
    assert taxonomy.role_key_for_term(title) == "hr_analyst"


def test_an_unknown_title_produces_no_title_evidence_at_all() -> None:
    """The failure mode behind the tie: absence, not a weak signal.

    "Workforce Planning Lead" is not in the taxonomy, so nothing is extracted
    from it and the role can only come from skills. That is what makes the
    tiebreak matter, and why the result has to say it needs review.
    """
    profile = analyze_resume_text(
        "SUMMARY\nWorkforce professional.\n\n"
        "EXPERIENCE\nWorkforce Planning Lead | Acme | 2020 - Present\n"
        "- Built headcount reports\n\nSKILLS\nExcel, SQL\n"
    )

    assert [title for title in profile.titles if title.role_key is not None] == []
    assert any("Review the target role" in warning for warning in profile.warnings)


# --- Exact ties ------------------------------------------------------------


def _tie_skills() -> list[ExtractedSkill]:
    """Skills giving recruiter and graphic_designer identical full coverage.

    Both roles have exactly three core skills and share none of them, so each
    scores 3/3 - an exact tie that no amount of evidence separates.
    """
    return _skills(
        *taxonomy.ROLES["recruiter"].core_skills,
        *taxonomy.ROLES["graphic_designer"].core_skills,
    )


def test_exact_tie_prefers_the_role_matching_the_detected_domain() -> None:
    detection = detect_target_role([], _tie_skills(), domain_hint="human_resources")

    assert detection.key == "recruiter"
    assert detection.tie_break == "domain_agreement"
    assert "graphic_designer" in detection.tied_with


def test_exact_tie_without_a_matching_domain_stays_deterministic() -> None:
    """No agreeing role, so the order must not depend on anything incidental."""
    first = detect_target_role([], _tie_skills(), domain_hint="finance")
    second = detect_target_role([], _tie_skills(), domain_hint="finance")

    assert first.key == second.key == "graphic_designer"  # alphabetical
    assert first.tie_break == "alphabetical"
    assert first.tied_with == ["recruiter"]


def test_exact_tie_with_no_hint_at_all_stays_deterministic() -> None:
    detection = detect_target_role([], _tie_skills())

    assert detection.key == "graphic_designer"
    assert detection.tie_break == "alphabetical"


def test_domain_agreement_never_overrides_a_higher_scoring_role() -> None:
    """A role beaten on evidence has been beaten, whatever its domain."""
    skills = _skills(
        *taxonomy.ROLES["recruiter"].core_skills,
        *taxonomy.ROLES["graphic_designer"].core_skills[:1],
    )

    detection = detect_target_role([], skills, domain_hint="design")

    assert detection.key == "recruiter"
    assert detection.tie_break is None
    assert detection.tied_with == []


def test_a_clear_winner_is_not_marked_as_tied() -> None:
    profile = analyze_resume_text(FINANCIAL_ANALYST_RESUME)

    assert profile.target_role.key == "financial_analyst"
    assert not profile.target_role.is_tied
    assert profile.target_role.tie_break is None
    assert not any(
        "Review the target role" in warning for warning in profile.warnings
    )


# --- The two reported resumes ----------------------------------------------


def test_people_analytics_resume_classifies_as_hr_not_business_operations() -> None:
    """The reported defect: this resume returned Business Operations Analyst."""
    profile = analyze_resume_text(HR_ANALYST_RESUME)

    assert profile.target_role.key == "hr_analyst"
    assert profile.domain.key == "human_resources"
    assert profile.target_role.confidence > 0.8
    assert not profile.target_role.is_tied
    # The titles that used to be invisible now carry the detection.
    assert {title.role_key for title in profile.titles} == {"hr_analyst"}
    assert all(
        query.lower().startswith(
            ("hr ", "human resources", "hris", "workforce", "people")
        )
        for query in profile.role_variations
    )


def test_network_engineer_resume_is_unchanged() -> None:
    """Guard against the adjacent-role regressions this resume used to hit."""
    profile = analyze_resume_text(NETWORK_ENGINEER_RESUME)

    assert profile.target_role.key == "network_engineer"
    assert profile.domain.key == "network_infrastructure"
    assert profile.target_role.confidence > 0.9


# --- Career boundaries -----------------------------------------------------


@pytest.mark.parametrize(
    "title",
    ["People Analytics Analyst", "HR Data Analyst", "HR Reporting Analyst"],
)
def test_hr_analytics_titles_are_not_data_analyst_jobs(title: str) -> None:
    """These were accepted as Data Analyst before HR analytics had a role."""
    assessment = evaluate_role(
        _profile("data_analyst"),
        extract_requirements(title=title, description=JOB_BODY),
    )

    assert not assessment.passed
    assert assessment.rejection is RoleRejection.ROLE_FAMILY_MISMATCH
    assert assessment.detected_role_key == "hr_analyst"


def test_hr_operations_analyst_is_not_a_business_operations_analyst_job() -> None:
    assessment = evaluate_role(
        _profile("business_operations_analyst"),
        extract_requirements(title="HR Operations Analyst", description=JOB_BODY),
    )

    assert not assessment.passed
    assert assessment.rejection is RoleRejection.ROLE_FAMILY_MISMATCH


def test_the_boundary_cuts_both_ways() -> None:
    """An HR Analyst resume must not be shown ordinary Data Analyst work."""
    assert _verdict("hr_analyst", "Data Analyst") == "REJECTED"
    assert _verdict("hr_analyst", "Business Operations Analyst") == "REJECTED"
    assert _verdict("hr_analyst", "HR Analyst") == "exact"
    assert _verdict("hr_analyst", "HRIS Analyst") == "exact"


def test_genuine_data_analyst_titles_still_pass() -> None:
    assert _verdict("data_analyst", "Data Analyst") == "exact"
    assert _verdict("data_analyst", "Reporting Analyst") == "exact"
    assert _verdict("data_analyst", "Marketing Data Analyst") == "strong_variant"


def test_business_operations_analyst_still_accepts_its_own_titles() -> None:
    assert _verdict("business_operations_analyst", "Operations Analyst") == "exact"
    assert (
        _verdict("business_operations_analyst", "Business Operations Analyst")
        == "exact"
    )


# --- Reachability: what the gate accepts, the queries must be able to find --


@pytest.mark.parametrize("title", ["Finance Analyst", "Budget Analyst"])
def test_finance_and_budget_analyst_are_now_reachable(title: str) -> None:
    """Both were accepted as EXACT by the gate and retrieved by no query."""
    queries = generate_role_variations("financial_analyst", Seniority.MID)

    assert _verdict("financial_analyst", title) == "exact"
    assert any(title_matches(query, title) for query in queries), queries


def test_hr_coordinator_is_now_reachable() -> None:
    """The spelled-out form cannot retrieve the abbreviated one."""
    queries = generate_role_variations("hr_generalist", Seniority.MID)

    assert _verdict("hr_generalist", "HR Coordinator") == "exact"
    assert not title_matches("Human Resources Coordinator", "HR Coordinator")
    assert any(title_matches(query, "HR Coordinator") for query in queries)


def test_added_variants_do_not_leak_into_another_role() -> None:
    for role_key in ("financial_analyst", "hr_generalist", "hr_analyst"):
        variations = generate_role_variations(role_key, Seniority.MID)
        assert variations_are_same_role(role_key, variations), role_key


# --- The query cap ---------------------------------------------------------


def _plan(role_key: str, seniority: Seniority | None) -> tuple[str, ...]:
    spec = taxonomy.get_role(role_key)
    assert spec is not None
    return build_query_plan(
        ResumeMatchProfile(
            resume_id="test",
            target_role_key=role_key,
            target_role_display=spec.canonical_title,
            domain_key=spec.domain_key,
            seniority=seniority,
            years_experience=5.0,
        )
    ).queries


def test_the_portal_cap_is_the_only_binding_limit() -> None:
    """Storage must not be the narrower of the two, or it truncates first."""
    assert DEFAULT_MAX_VARIATIONS > DEFAULT_MAX_QUERIES_PER_PORTAL


def test_the_portal_cap_is_still_enforced() -> None:
    for seniority in Seniority:
        for role_key in taxonomy.ROLES:
            queries = _plan(role_key, seniority)
            assert len(queries) <= DEFAULT_MAX_QUERIES_PER_PORTAL
            assert len(queries) == len(set(queries))


def test_the_canonical_title_is_always_the_first_query() -> None:
    for seniority in Seniority:
        for role_key, spec in taxonomy.ROLES.items():
            assert _plan(role_key, seniority)[0] == spec.canonical_title


@pytest.mark.parametrize(
    ("role_key", "seniority", "expected"),
    [
        ("financial_analyst", Seniority.SENIOR, "Senior Financial Analyst"),
        ("financial_analyst", Seniority.JUNIOR, "Junior Financial Analyst"),
        ("network_engineer", Seniority.SENIOR, "Senior Network Engineer"),
        ("security_analyst", Seniority.SENIOR, "Senior Security Analyst"),
        ("accountant", Seniority.SENIOR, "Senior Accountant"),
        ("recruiter", Seniority.SENIOR, "Senior Recruiter"),
        ("hr_analyst", Seniority.SENIOR, "Senior HR Analyst"),
    ],
)
def test_a_seniority_query_is_never_truncated_away(
    role_key: str, seniority: Seniority, expected: str
) -> None:
    """These were generated, then dropped by the cap for being appended last."""
    assert expected in _plan(role_key, seniority)


def test_no_seniority_query_is_lost_for_any_role() -> None:
    """The general form of the case above, over the whole taxonomy."""
    lost: list[tuple[str, str, str]] = []
    for seniority in Seniority:
        for role_key in taxonomy.ROLES:
            generated = generate_role_variations(role_key, seniority)
            sent = _plan(role_key, seniority)
            base = set(generate_role_variations(role_key, None))
            lost.extend(
                (role_key, seniority.value, query)
                for query in generated
                if query not in base and query not in sent
            )

    assert lost == []


def test_a_mid_level_resume_adds_no_seniority_query() -> None:
    """Prefixes are additive only; mid is titled plainly and stays that way."""
    assert all(
        not query.lower().startswith(("senior ", "junior ", "lead ", "principal "))
        for query in _plan("financial_analyst", Seniority.MID)
    )
