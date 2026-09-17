"""Requirement extraction, skills, experience and the final score.

Everything here is pure: no database, no network, no clock. That is the point
- the same resume and job must always produce the same number and the same
explanation, and a test that needed a fixture database could not prove it.
"""

from __future__ import annotations

import pytest

from app.analysis import taxonomy
from app.analysis.text import normalize_title
from app.core.constants import Seniority
from app.matching.experience import evaluate_experience
from app.matching.profile import ResumeMatchProfile
from app.matching.requirements import (
    MIN_SCOREABLE_DESCRIPTION,
    SkillContext,
    extract_requirements,
    resume_degree_level,
    resume_has_certification,
)
from app.matching.scoring import (
    COMPONENT_WEIGHTS,
    MAX_SCORE,
    QUALIFICATION_THRESHOLD,
    RejectionCode,
    round_score,
    score_match,
)
from app.matching.skills import GENERIC_SKILL_KEYS, evaluate_skills
from app.matching.version import SCORING_VERSION

PADDING = "Additional context about the team and the work. " * 8


def make_profile(
    *,
    role_key: str = "financial_analyst",
    seniority: Seniority | None = Seniority.MID,
    years: float | None = 4.0,
    skills: set[str] | None = None,
    education: tuple[str, ...] = ("bachelor of science in finance",),
    certifications: tuple[str, ...] = (),
) -> ResumeMatchProfile:
    spec = taxonomy.get_role(role_key)
    assert spec is not None

    return ResumeMatchProfile(
        resume_id="resume",
        target_role_key=role_key,
        target_role_display=spec.canonical_title,
        domain_key=spec.domain_key,
        seniority=seniority,
        years_experience=years,
        skill_keys=frozenset(
            skills
            if skills is not None
            else {"excel", "sql", "financial modeling", "forecasting", "budgeting"}
        ),
        role_variation_keys=frozenset(
            {normalize_title(v) for v in spec.variations}
            | set(spec.aliases)
            | {normalize_title(spec.canonical_title)}
        ),
        education=education,
        certifications=certifications,
    )


def describe(body: str) -> str:
    """Pad a description past the scoreable minimum."""
    return f"{body}\n\n{PADDING}"


# --- Requirement extraction -------------------------------------------------


def test_required_and_preferred_skills_are_separated_by_their_heading() -> None:
    job = extract_requirements(
        title="Financial Analyst",
        description=describe(
            "Requirements:\n- Advanced Excel\n- SQL\n\n"
            "Preferred qualifications:\n- Power BI\n- Tableau"
        ),
    )

    assert {s.skill_key for s in job.required_skills} == {"excel", "sql"}
    assert {s.skill_key for s in job.preferred_skills} == {"power bi", "tableau"}


def test_an_inline_softener_beats_a_requirements_heading() -> None:
    # "SQL required, Python preferred" is one of each.
    job = extract_requirements(
        title="Financial Analyst",
        description=describe("Requirements:\n- SQL\n- Python preferred"),
    )

    assert {s.skill_key for s in job.required_skills} == {"sql"}
    assert {s.skill_key for s in job.preferred_skills} == {"python"}


def test_a_skill_mentioned_in_passing_is_not_a_requirement() -> None:
    # A posting describing the work is not listing must-haves.
    job = extract_requirements(
        title="Financial Analyst",
        description=describe("About the role\nYou will build models in Excel."),
    )

    assert job.required_skills == ()
    assert job.preferred_skills == ()
    assert {s.skill_key for s in job.supporting_skills} == {"excel"}


def test_only_unambiguous_wording_makes_a_skill_mandatory() -> None:
    job = extract_requirements(
        title="Financial Analyst",
        description=describe(
            "Requirements:\n- Must have advanced Excel\n- Experience with SQL"
        ),
    )

    mandatory = {s.skill_key for s in job.mandatory_skills}
    assert mandatory == {"excel"}
    # SQL is still required, just not a stated must-have.
    assert {s.skill_key for s in job.required_skills} == {"excel", "sql"}


def test_ordinary_words_do_not_become_skills() -> None:
    # Not every word in a description is a skill.
    job = extract_requirements(
        title="Financial Analyst",
        description=describe(
            "Requirements:\n- Excellent communication and strong attention to "
            "detail\n- A proactive, collaborative mindset"
        ),
    )

    assert all(taxonomy.get_skill(s.skill_key) for s in job.skills)


@pytest.mark.parametrize(
    ("written", "canonical"),
    [
        ("Microsoft Excel", "excel"),
        ("Postgres", "postgresql"),
        ("PostgreSQL", "postgresql"),
        ("Microsoft Power BI", "power bi"),
        ("Power BI", "power bi"),
    ],
)
def test_safe_aliases_resolve_to_one_skill(written: str, canonical: str) -> None:
    job = extract_requirements(
        title="Financial Analyst",
        description=describe(f"Requirements:\n- {written}"),
    )

    assert canonical in {s.skill_key for s in job.required_skills}


@pytest.mark.parametrize(
    ("text", "expected_min", "expected_max"),
    [
        ("Requirements:\n- 3+ years of experience", 3.0, None),
        ("Requirements:\n- At least 5 years of experience", 5.0, None),
        ("Requirements:\n- Minimum of 2 years", 2.0, None),
        ("Requirements:\n- 3-5 years of experience", 3.0, 5.0),
        ("Requirements:\n- A degree in finance", None, None),
    ],
)
def test_stated_experience_is_read_and_silence_is_left_alone(
    text: str, expected_min: float | None, expected_max: float | None
) -> None:
    job = extract_requirements(title="Financial Analyst", description=describe(text))

    assert job.min_years == expected_min
    assert job.max_years == expected_max


def test_a_short_description_is_flagged_as_unscoreable() -> None:
    short = extract_requirements(title="Financial Analyst", description="Apply now.")
    full = extract_requirements(
        title="Financial Analyst", description="x" * MIN_SCOREABLE_DESCRIPTION
    )

    assert short.has_scoreable_description is False
    assert full.has_scoreable_description is True


def test_a_hedged_degree_is_not_mandatory() -> None:
    hedged = extract_requirements(
        title="Financial Analyst",
        description=describe("Bachelor's degree preferred"),
    )
    required = extract_requirements(
        title="Financial Analyst",
        description=describe("Bachelor's degree is required"),
    )

    assert hedged.degree_is_mandatory is False
    assert required.degree_is_mandatory is True


def test_certifications_are_read_and_their_firmness_recorded() -> None:
    job = extract_requirements(
        title="Financial Analyst",
        description=describe("Requirements:\n- CPA is required\n- CFA preferred"),
    )

    assert set(job.required_certifications) == {"cpa", "cfa"}
    assert set(job.mandatory_certifications) == {"cpa"}


def test_resume_credentials_are_matched_against_requirements() -> None:
    assert resume_has_certification(("certified public accountant",), "cpa") is True
    assert resume_has_certification(("cfa charterholder",), "cpa") is False
    assert resume_degree_level(("master of business administration",)) == 3
    assert resume_degree_level(()) is None


# --- Skills -----------------------------------------------------------------


def test_matched_and_missing_required_skills_are_reported() -> None:
    profile = make_profile(skills={"excel", "sql"})
    job = extract_requirements(
        title="Financial Analyst",
        description=describe("Requirements:\n- Excel\n- SQL\n- Tableau"),
    )

    assessment = evaluate_skills(profile, job)

    assert set(assessment.matched_required) == {"Microsoft Excel", "SQL"}
    assert set(assessment.missing_required) == {"Tableau"}
    assert assessment.required_coverage == pytest.approx(2 / 3)


def test_preferred_skills_score_separately_from_required_ones() -> None:
    profile = make_profile(skills={"excel", "sql", "power bi"})
    job = extract_requirements(
        title="Financial Analyst",
        description=describe(
            "Requirements:\n- Excel\n- SQL\n\nPreferred:\n- Power BI\n- Tableau"
        ),
    )

    assessment = evaluate_skills(profile, job)

    assert assessment.required_coverage == 1.0
    assert assessment.preferred_coverage == 0.5


def test_a_missing_mandatory_skill_is_recorded_separately() -> None:
    profile = make_profile(skills={"excel"})
    job = extract_requirements(
        title="Financial Analyst",
        description=describe("Requirements:\n- Must have Python\n- Excel"),
    )

    assessment = evaluate_skills(profile, job)

    assert assessment.missing_mandatory == ("Python",)
    assert assessment.has_missing_mandatory is True


def test_a_missing_non_mandatory_skill_costs_points_but_does_not_reject() -> None:
    profile = make_profile(skills={"excel"})
    job = extract_requirements(
        title="Financial Analyst",
        description=describe("Requirements:\n- Excel\n- Experience with Tableau"),
    )

    assessment = evaluate_skills(profile, job)

    assert assessment.missing_required == ("Tableau",)
    assert assessment.has_missing_mandatory is False


def test_a_posting_naming_no_skills_gets_neutral_credit_not_zero() -> None:
    # Punishing the candidate for a vague advert would be wrong; the role gate
    # has already established this is the right job.
    profile = make_profile()
    job = extract_requirements(
        title="Financial Analyst",
        description=describe("A great opportunity with a growing team."),
    )

    assessment = evaluate_skills(profile, job)

    assert assessment.no_skills_stated is True
    assert 0.0 < assessment.required_coverage < 1.0


def test_missing_skills_are_reported_as_penalties_not_as_evidence() -> None:
    profile = make_profile(skills={"excel"})
    job = extract_requirements(
        title="Financial Analyst",
        description=describe("Requirements:\n- Excel\n- Tableau"),
    )

    assessment = evaluate_skills(profile, job)

    assert any("Missing required" in note for note in assessment.penalties)
    assert not any("Missing" in note for note in assessment.evidence)


# --- The generic-shortfall floor --------------------------------------------
#
# Coverage is a plain fraction, so "Communication" used to weigh as much as
# "Kubernetes". A posting naming NO skill scored the neutral 0.7 while one
# naming a single generic skill the resume did not list scored 0.0 - a
# 21-point gap decided by whether the employer wrote one recognisable word.
# The floor closes that gap and does nothing else: it needs EVERY missing
# requirement to be generic, it never reaches past neutral, and it never
# lowers anything.


def test_the_generic_set_is_derived_from_the_taxonomy_not_hand_listed() -> None:
    # The point of deriving it is that a skill added to skills_data.py is
    # classified by the domains it declares, with nothing to keep in step.
    assert GENERIC_SKILL_KEYS == {
        key for key, spec in taxonomy.SKILLS.items() if not spec.domains
    }
    assert "communication" in GENERIC_SKILL_KEYS
    # Every real skill must stay out of it, or the floor would hide a real gap.
    for key in ("sql", "python", "gaap", "cisco", "aws", "power bi", "excel", "hris"):
        assert key not in GENERIC_SKILL_KEYS, key


def test_a_shortfall_of_one_generic_skill_is_floored_to_neutral() -> None:
    profile = make_profile(skills={"excel", "forecasting"})
    job = extract_requirements(
        title="Financial Analyst",
        description=describe("Requirements:\n- Strong communication skills"),
    )

    assessment = evaluate_skills(profile, job)

    assert assessment.missing_required == ("Communication",)
    assert assessment.required_coverage == pytest.approx(0.7)


def test_a_shortfall_of_several_generic_skills_is_floored_to_neutral() -> None:
    profile = make_profile(skills={"excel", "forecasting"})
    job = extract_requirements(
        title="Financial Analyst",
        description=describe(
            "Requirements:\n- Communication\n- Teamwork\n- Time management"
        ),
    )

    assessment = evaluate_skills(profile, job)

    assert set(assessment.missing_required) == {
        "Communication",
        "Teamwork",
        "Time Management",
    }
    assert assessment.required_coverage == pytest.approx(0.7)


def test_a_mixed_shortfall_is_not_floored() -> None:
    # The case that rules out the looser candidate rule: crediting the matched
    # Communication here would make the missing Excel cost nothing at all.
    profile = make_profile(skills={"communication", "forecasting"})
    job = extract_requirements(
        title="Financial Analyst",
        description=describe(
            "Requirements:\n- Advanced Excel\n- Strong communication skills"
        ),
    )

    assessment = evaluate_skills(profile, job)

    assert assessment.missing_required == ("Microsoft Excel",)
    assert assessment.required_coverage == pytest.approx(0.5)


def test_a_real_skill_shortfall_is_unchanged() -> None:
    profile = make_profile(skills={"excel", "forecasting"})
    job = extract_requirements(
        title="Financial Analyst",
        description=describe("Requirements:\n- Advanced SQL"),
    )

    assessment = evaluate_skills(profile, job)

    assert assessment.missing_required == ("SQL",)
    assert assessment.required_coverage == 0.0


def test_a_mandatory_generic_skill_still_rejects() -> None:
    # The floor moves a score. It must not move an outcome: mandatory skills
    # are handled before the threshold and are not part of this arithmetic.
    profile = make_profile(skills={"excel", "forecasting"})
    job = extract_requirements(
        title="Financial Analyst",
        description=describe(
            "Requirements:\n- Must have exceptional communication skills"
        ),
    )

    result = score_match(profile, job)

    assert result.skills.required_coverage == pytest.approx(0.7)
    assert result.skills.missing_mandatory == ("Communication",)
    assert result.qualified is False
    assert result.rejection_code is RejectionCode.MISSING_MANDATORY_SKILL


def test_the_floor_never_lifts_coverage_above_neutral() -> None:
    profile = make_profile(skills={"excel"})
    job = extract_requirements(
        title="Financial Analyst",
        description=describe(
            "Requirements:\n- Excel\n- Communication\n- Teamwork\n- Leadership"
        ),
    )

    assessment = evaluate_skills(profile, job)

    # 1 of 4 matched would be 0.25; the floor lifts it to neutral, no further.
    assert assessment.required_coverage == pytest.approx(0.7)
    assert assessment.required_coverage <= 0.7


def test_the_floor_never_lowers_an_already_better_score() -> None:
    # A posting whose generic requirements are all MET keeps full credit; the
    # floor is a floor, not a replacement.
    profile = make_profile(skills={"communication", "teamwork", "excel"})
    job = extract_requirements(
        title="Financial Analyst",
        description=describe("Requirements:\n- Communication\n- Teamwork"),
    )

    assessment = evaluate_skills(profile, job)

    assert assessment.missing_required == ()
    assert assessment.required_coverage == 1.0


def test_the_floor_moves_a_real_posting_across_the_threshold() -> None:
    # End to end through score_match, because the component in isolation does
    # not show that this is worth 21 points.
    profile = make_profile(skills={"excel", "forecasting", "budgeting"})
    job = extract_requirements(
        title="Senior Financial Analyst",
        description=describe(
            "Requirements:\n- 3+ years of experience\n"
            "- Strong communication skills"
        ),
    )

    result = score_match(profile, job)

    assert result.skills.required_coverage == pytest.approx(0.7)
    assert result.final_score >= QUALIFICATION_THRESHOLD
    assert result.qualified is True


def test_the_floor_does_not_rescue_a_posting_missing_a_real_skill() -> None:
    profile = make_profile(skills={"excel", "forecasting", "budgeting"})
    job = extract_requirements(
        title="Senior Financial Analyst",
        description=describe(
            "Requirements:\n- 3+ years of experience\n- Advanced SQL\n"
            "- Strong communication skills"
        ),
    )

    result = score_match(profile, job)

    assert "SQL" in result.skills.missing_required
    assert result.skills.required_coverage < 0.7
    assert result.qualified is False
    assert result.rejection_code is RejectionCode.BELOW_THRESHOLD


# --- Experience and seniority ----------------------------------------------


def test_meeting_a_stated_requirement_scores_full() -> None:
    profile = make_profile(years=5.0, seniority=Seniority.MID)
    job = extract_requirements(
        title="Financial Analyst",
        description=describe("Requirements:\n- 3+ years of experience"),
    )

    assessment = evaluate_experience(profile, job)

    assert assessment.years_factor == 1.0
    assert assessment.insufficient_experience is False


def test_a_small_shortfall_is_a_penalty_not_a_rejection() -> None:
    profile = make_profile(years=4.0)
    job = extract_requirements(
        title="Financial Analyst",
        description=describe("Requirements:\n- 5+ years of experience"),
    )

    assessment = evaluate_experience(profile, job)

    assert assessment.insufficient_experience is False
    assert assessment.penalties


def test_a_large_shortfall_rejects() -> None:
    profile = make_profile(years=2.0)
    job = extract_requirements(
        title="Financial Analyst",
        description=describe("Requirements:\n- 10+ years of experience"),
    )

    assessment = evaluate_experience(profile, job)

    assert assessment.insufficient_experience is True
    assert assessment.shortfall_years == 8.0


def test_a_posting_with_no_stated_experience_is_never_rejected_for_it() -> None:
    # Entry-level adverts routinely give no number at all.
    profile = make_profile(years=0.5, seniority=Seniority.ENTRY)
    job = extract_requirements(
        title="Financial Analyst",
        description=describe("Requirements:\n- A degree in finance\n- Strong Excel"),
    )

    assessment = evaluate_experience(profile, job)

    assert assessment.insufficient_experience is False
    assert assessment.years_factor == 1.0


def test_a_resume_without_stated_years_is_not_treated_as_zero() -> None:
    profile = make_profile(years=None)
    job = extract_requirements(
        title="Financial Analyst",
        description=describe("Requirements:\n- 5+ years of experience"),
    )

    assessment = evaluate_experience(profile, job)

    assert assessment.insufficient_experience is False
    assert 0.5 < assessment.years_factor < 1.0


def test_being_more_senior_than_the_posting_costs_less_than_being_less_senior() -> None:
    senior_resume = make_profile(seniority=Seniority.SENIOR, years=9.0)
    junior_resume = make_profile(seniority=Seniority.JUNIOR, years=9.0)
    job = extract_requirements(title="Financial Analyst", description=describe("Role."))

    over = evaluate_experience(senior_resume, job)
    under = evaluate_experience(junior_resume, job)

    assert over.seniority_factor >= under.seniority_factor


# --- The final score --------------------------------------------------------


def test_the_component_weights_total_one_hundred() -> None:
    assert sum(COMPONENT_WEIGHTS.values()) == MAX_SCORE


def test_a_strong_same_role_match_qualifies() -> None:
    profile = make_profile(
        skills={"excel", "sql", "forecasting", "budgeting", "financial modeling"}
    )
    job = extract_requirements(
        title="Senior Financial Analyst",
        description=describe(
            "Requirements:\n- 3+ years of experience\n- Excel\n- SQL\n"
            "- Forecasting and budgeting"
        ),
    )

    result = score_match(profile, job)

    assert result.qualified is True
    assert result.final_score >= QUALIFICATION_THRESHOLD
    assert result.rejection_code is None
    assert result.positive_evidence


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Accountant", RejectionCode.ROLE_FAMILY_MISMATCH),
        ("Data Analyst", RejectionCode.ROLE_FAMILY_MISMATCH),
        ("Investment Banker", RejectionCode.ROLE_INCOMPATIBLE),
    ],
)
def test_a_wrong_role_is_rejected_however_well_the_skills_overlap(
    title: str, expected: RejectionCode
) -> None:
    # The whole point of the gate: perfect skills, wrong job.
    profile = make_profile(
        skills={"excel", "sql", "forecasting", "budgeting", "financial modeling"}
    )
    job = extract_requirements(
        title=title,
        description=describe(
            "Requirements:\n- Excel\n- SQL\n- Forecasting\n- Budgeting\n"
            "- Financial modeling"
        ),
    )

    result = score_match(profile, job)

    assert result.qualified is False
    assert result.rejection_code is expected
    assert result.component_scores["role"] == 0.0
    # And the score is reported honestly, not massaged to justify the outcome.
    assert result.final_score > 0.0


def test_a_role_incompatible_job_can_never_reach_the_threshold_on_skills() -> None:
    # Role carries 30 points, so a zero there caps the total at 70 - and the
    # hard rejection applies regardless.
    profile = make_profile(
        skills={"excel", "sql", "forecasting", "budgeting", "financial modeling"}
    )
    job = extract_requirements(
        title="Accountant",
        description=describe("Requirements:\n- Excel\n- SQL"),
    )

    result = score_match(profile, job)

    assert result.final_score <= MAX_SCORE - COMPONENT_WEIGHTS["role"]


def test_an_unscoreable_description_is_rejected_rather_than_guessed_at() -> None:
    profile = make_profile()
    job = extract_requirements(title="Financial Analyst", description="Apply today.")

    result = score_match(profile, job)

    assert result.qualified is False
    assert result.rejection_code is RejectionCode.INSUFFICIENT_DESCRIPTION


def test_a_missing_mandatory_skill_rejects_with_the_skill_named() -> None:
    profile = make_profile(skills={"excel"})
    job = extract_requirements(
        title="Financial Analyst",
        description=describe("Requirements:\n- Must have Python\n- Excel"),
    )

    result = score_match(profile, job)

    assert result.rejection_code is RejectionCode.MISSING_MANDATORY_SKILL
    assert "Python" in (result.rejection_reason or "")


def test_a_missing_mandatory_certification_rejects() -> None:
    profile = make_profile(certifications=())
    job = extract_requirements(
        title="Financial Analyst",
        description=describe("Requirements:\n- CPA is required"),
    )

    result = score_match(profile, job)

    assert result.rejection_code is RejectionCode.MISSING_MANDATORY_CERTIFICATION


def test_holding_the_required_certification_does_not_reject() -> None:
    profile = make_profile(certifications=("certified public accountant, cpa",))
    job = extract_requirements(
        title="Financial Analyst",
        description=describe("Requirements:\n- CPA is required\n- 2+ years"),
    )

    result = score_match(profile, job)

    assert result.rejection_code is not RejectionCode.MISSING_MANDATORY_CERTIFICATION


def test_a_hedged_certification_never_rejects() -> None:
    profile = make_profile(certifications=())
    job = extract_requirements(
        title="Financial Analyst",
        description=describe("Requirements:\n- 2+ years\n- CFA preferred"),
    )

    result = score_match(profile, job)

    assert result.rejection_code is not RejectionCode.MISSING_MANDATORY_CERTIFICATION


def test_a_mandatory_degree_the_resume_lacks_rejects() -> None:
    profile = make_profile(education=())
    job = extract_requirements(
        title="Financial Analyst",
        description=describe("Requirements:\n- Bachelor's degree is required"),
    )

    result = score_match(profile, job)

    assert result.rejection_code is RejectionCode.INSUFFICIENT_EDUCATION


def test_education_relevance_is_credited_when_the_resume_meets_it() -> None:
    with_degree = score_match(
        make_profile(education=("master of science in finance",)),
        extract_requirements(
            title="Financial Analyst",
            description=describe("Requirements:\n- Bachelor's degree required\n- Excel"),
        ),
    )
    without = score_match(
        make_profile(education=()),
        extract_requirements(
            title="Financial Analyst",
            description=describe("Requirements:\n- Bachelor's degree preferred\n- Excel"),
        ),
    )

    assert (
        with_degree.component_scores["domain_education"]
        > without.component_scores["domain_education"]
    )


def test_a_required_clearance_the_resume_lacks_rejects() -> None:
    profile = make_profile()
    job = extract_requirements(
        title="Financial Analyst",
        description=describe(
            "Requirements:\n- Active security clearance is required\n- Excel"
        ),
    )

    result = score_match(profile, job)

    assert result.rejection_code is RejectionCode.MISSING_SECURITY_CLEARANCE


# --- Threshold and rounding -------------------------------------------------


def test_the_threshold_is_the_locked_seventy() -> None:
    assert QUALIFICATION_THRESHOLD == 70.0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (69.994, 69.99),
        (69.995, 70.0),
        (70.0, 70.0),
        (70.004, 70.0),
        (99.999, 100.0),
    ],
)
def test_rounding_is_half_up_and_deterministic(raw: float, expected: float) -> None:
    assert round_score(raw) == expected


def test_exactly_seventy_qualifies_and_a_hair_under_does_not() -> None:
    # The boundary itself, stated as a rule rather than inferred from a score.
    assert round_score(70.0) >= QUALIFICATION_THRESHOLD
    assert round_score(69.99) < QUALIFICATION_THRESHOLD


def test_a_score_below_the_threshold_is_rejected_with_a_code() -> None:
    # Weak on every component but still the right role.
    profile = make_profile(
        skills=set(), years=None, seniority=Seniority.JUNIOR, education=()
    )
    job = extract_requirements(
        title="Financial Analyst",
        description=describe(
            "Requirements:\n- Excel\n- SQL\n- Tableau\n- Python\n- SAP\n"
            "\nPreferred:\n- Power BI\n- R"
        ),
    )

    result = score_match(profile, job)

    assert result.final_score < QUALIFICATION_THRESHOLD
    assert result.qualified is False
    assert result.rejection_code is RejectionCode.BELOW_THRESHOLD


def test_the_score_is_never_pushed_up_to_reach_the_threshold() -> None:
    profile = make_profile(skills=set(), years=None, education=())
    job = extract_requirements(
        title="Financial Analyst",
        description=describe("Requirements:\n- Excel\n- SQL\n- Tableau\n- Python"),
    )

    result = score_match(profile, job)

    assert result.final_score == round_score(
        sum(result.component_scores.values())
    )


# --- Determinism ------------------------------------------------------------


def test_repeated_scoring_produces_an_identical_result() -> None:
    profile = make_profile()
    job = extract_requirements(
        title="Senior Financial Analyst",
        description=describe("Requirements:\n- 3+ years\n- Excel\n- SQL"),
    )

    first = score_match(profile, job)
    second = score_match(profile, job)

    assert first.final_score == second.final_score
    assert first.component_scores == second.component_scores
    assert first.positive_evidence == second.positive_evidence
    assert first.penalties == second.penalties
    assert first.to_details() == second.to_details()


def test_every_result_records_the_scoring_version() -> None:
    result = score_match(
        make_profile(),
        extract_requirements(title="Financial Analyst", description=describe("Role.")),
    )

    assert result.scoring_version == SCORING_VERSION
    assert result.to_details()["scoring_version"] == SCORING_VERSION


def test_the_explanation_covers_every_component() -> None:
    result = score_match(
        make_profile(),
        extract_requirements(
            title="Financial Analyst",
            description=describe("Requirements:\n- 3+ years\n- Excel"),
        ),
    )
    details = result.to_details()

    assert set(details["components"]) == set(COMPONENT_WEIGHTS)
    assert details["role"]["reason"]
    assert details["experience"]["summary"]


def test_scoring_uses_no_language_model() -> None:
    # CLAUDE.md section 7: an LLM may assist extraction, but deterministic
    # validation is authoritative, and this engine does not call one at all.
    from app.core.config import get_settings

    assert get_settings().enable_llm_assist is False


def test_skill_context_values_are_the_three_documented_kinds() -> None:
    assert {context.value for context in SkillContext} == {
        "required",
        "preferred",
        "supporting",
    }
