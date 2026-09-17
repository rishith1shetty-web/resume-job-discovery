"""The role/title gate.

The gate is what stops a Financial Analyst resume matching an Accountant
posting because they both mention Excel. Every test here is about a title
decision, and most are about a title that must be *refused*.

No database and no network.
"""

from __future__ import annotations

import pytest

from app.analysis import taxonomy
from app.analysis.text import normalize_title
from app.core.constants import Seniority
from app.matching.profile import ResumeMatchProfile
from app.matching.requirements import extract_requirements, strip_seniority
from app.matching.roles import RoleRejection, RoleVerdict, evaluate_role

DESCRIPTION = "A description long enough to be scoreable. " * 10


def profile_for(
    role_key: str, *, seniority: Seniority | None = Seniority.MID
) -> ResumeMatchProfile:
    spec = taxonomy.get_role(role_key)
    assert spec is not None

    return ResumeMatchProfile(
        resume_id="resume",
        target_role_key=role_key,
        target_role_display=spec.canonical_title,
        domain_key=spec.domain_key,
        seniority=seniority,
        years_experience=4.0,
        role_variation_keys=frozenset(
            {normalize_title(v) for v in spec.variations}
            | set(spec.aliases)
            | {normalize_title(spec.canonical_title)}
        ),
    )


def assess(role_key: str, title: str, *, seniority: Seniority | None = Seniority.MID):
    job = extract_requirements(title=title, description=DESCRIPTION)
    return evaluate_role(profile_for(role_key, seniority=seniority), job)


# --- Titles that must pass --------------------------------------------------


@pytest.mark.parametrize(
    "title",
    ["Financial Analyst", "financial analyst", "FINANCIAL ANALYST"],
)
def test_the_exact_target_role_passes(title: str) -> None:
    assessment = assess("financial_analyst", title)

    assert assessment.verdict is RoleVerdict.EXACT
    assert assessment.factor == 1.0


@pytest.mark.parametrize(
    "title",
    [
        "Senior Financial Analyst",
        "Sr. Financial Analyst",
        "Junior Financial Analyst",
        "Financial Analyst II",
        "Financial Analyst (Remote)",
        "Financial Analyst - Full Time",
    ],
)
def test_seniority_and_arrangement_do_not_change_the_role(title: str) -> None:
    # These are the same job advertised differently, not different jobs.
    assert assess("financial_analyst", title).verdict is RoleVerdict.EXACT


@pytest.mark.parametrize(
    "title",
    [
        "FP&A Analyst",
        "Financial Planning Analyst",
        "Corporate Financial Analyst",
        "Financial Reporting Analyst",
    ],
)
def test_approved_same_role_variations_pass(title: str) -> None:
    assessment = assess("financial_analyst", title)

    assert assessment.passed
    assert assessment.factor == 1.0


def test_a_qualified_form_of_the_role_passes_with_a_small_penalty() -> None:
    assessment = assess("financial_analyst", "Financial Analyst, Service Lines")

    assert assessment.verdict is RoleVerdict.STRONG_VARIANT
    assert 0.8 < assessment.factor < 1.0


def test_an_extra_specialisation_passes_with_a_larger_penalty() -> None:
    assessment = assess("financial_analyst", "Financial Revenue Analyst II")

    assert assessment.verdict is RoleVerdict.WEAK_VARIANT
    assert 0.6 < assessment.factor < 0.9


def test_remote_and_hybrid_wording_does_not_reduce_the_role_score() -> None:
    plain = assess("financial_analyst", "Financial Analyst")
    remote = assess("financial_analyst", "Financial Analyst - Remote")
    hybrid = assess("financial_analyst", "Financial Analyst (Hybrid)")

    assert remote.factor == plain.factor == hybrid.factor


# --- Titles that must be refused -------------------------------------------


@pytest.mark.parametrize(
    ("target", "title"),
    [
        # The brief's named cases.
        ("financial_analyst", "Accountant"),
        ("financial_analyst", "Business Analyst"),
        ("financial_analyst", "Data Analyst"),
        ("data_analyst", "Data Engineer"),
        ("data_analyst", "Data Scientist"),
        ("software_engineer", "DevOps Engineer"),
        # And a few more professions that share vocabulary.
        ("financial_analyst", "Auditor"),
        ("financial_analyst", "Financial Advisor"),
        ("software_engineer", "QA Engineer"),
    ],
)
def test_an_adjacent_role_is_refused(target: str, title: str) -> None:
    assessment = assess(target, title)

    assert assessment.verdict is RoleVerdict.INCOMPATIBLE
    assert assessment.rejection is RoleRejection.ROLE_FAMILY_MISMATCH


@pytest.mark.parametrize(
    ("target", "title"),
    [
        ("financial_analyst", "Investment Banker"),
        ("financial_analyst", "Registered Nurse"),
        ("financial_analyst", "Crypto Analyst"),
        ("software_engineer", "Network Engineer"),
    ],
)
def test_an_unrelated_role_is_refused(target: str, title: str) -> None:
    assessment = assess(target, title)

    assert assessment.verdict is RoleVerdict.INCOMPATIBLE
    assert assessment.rejection in {
        RoleRejection.ROLE_FAMILY_MISMATCH,
        RoleRejection.ROLE_INCOMPATIBLE,
    }


def test_a_foreign_role_inside_a_longer_title_is_refused() -> None:
    # The fuzzy tiers must never see this title.
    assessment = assess("financial_analyst", "Senior Data Engineer, Finance Team")

    assert assessment.verdict is RoleVerdict.INCOMPATIBLE
    assert assessment.detected_role_key == "data_engineer"


def test_a_different_profession_is_refused_as_such_not_as_a_seniority_problem() -> None:
    # "Staff Accountant" is an established title, not a senior accountant.
    assessment = assess("financial_analyst", "Staff Accountant")

    assert assessment.rejection is RoleRejection.ROLE_FAMILY_MISMATCH


# --- Leadership responsibility ---------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Director of Financial Planning",
        "VP of Finance",
        "Head of Financial Analysis",
    ],
)
def test_leadership_responsibility_well_above_the_candidate_is_refused(
    title: str,
) -> None:
    assessment = assess("financial_analyst", title, seniority=Seniority.MID)

    assert assessment.verdict is RoleVerdict.INCOMPATIBLE
    assert assessment.rejection in {
        RoleRejection.LEADERSHIP_MISMATCH,
        RoleRejection.ROLE_FAMILY_MISMATCH,
    }


def test_one_level_of_stretch_is_allowed() -> None:
    # A senior candidate seeing a lead posting is a stretch worth showing.
    assessment = assess(
        "financial_analyst", "Lead Financial Analyst", seniority=Seniority.SENIOR
    )

    assert assessment.passed


def test_leadership_is_not_refused_when_the_resume_states_no_seniority() -> None:
    # Rejecting here would be rejecting on a guess.
    assessment = assess(
        "financial_analyst", "Lead Financial Analyst", seniority=None
    )

    assert assessment.passed


# --- Structural guarantees --------------------------------------------------


def test_a_resume_without_a_target_role_matches_nothing() -> None:
    # Otherwise every job would pass the gate by default.
    profile = ResumeMatchProfile(
        resume_id="resume",
        target_role_key=None,
        target_role_display=None,
        domain_key="finance",
        seniority=Seniority.MID,
        years_experience=4.0,
    )
    job = extract_requirements(title="Financial Analyst", description=DESCRIPTION)

    assessment = evaluate_role(profile, job)

    assert assessment.verdict is RoleVerdict.INCOMPATIBLE
    assert assessment.rejection is RoleRejection.NO_TARGET_ROLE


def test_every_refusal_explains_itself() -> None:
    for title in ("Accountant", "Data Analyst", "Registered Nurse"):
        assessment = assess("financial_analyst", title)

        assert assessment.reason
        assert assessment.rejection is not None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Senior Financial Analyst", "financial analyst"),
        ("Sr. Financial Analyst (Remote)", "financial analyst"),
        ("Financial Analyst II", "financial analyst"),
        ("Junior Financial Analyst - Contract", "financial analyst"),
        # "Staff" is part of the title, not a seniority prefix.
        ("Staff Accountant", "staff accountant"),
    ],
)
def test_seniority_stripping_leaves_the_role_itself(raw: str, expected: str) -> None:
    assert strip_seniority(raw) == expected


# --- Comma-inverted titles --------------------------------------------------
#
# Some employers write the head noun first and qualify it after a comma. Every
# title in this section was captured from a live posting during the recall
# audit; the ones that must stay refused are the audit's own noise cases.


@pytest.mark.parametrize(
    ("role_key", "title", "expected"),
    [
        ("financial_analyst", "Analyst, FP&A", RoleVerdict.EXACT),
        (
            "machine_learning_engineer",
            "Engineer, Machine Learning",
            RoleVerdict.EXACT,
        ),
        ("financial_analyst", "Senior Analyst, Finance", RoleVerdict.EXACT),
        (
            "data_analyst",
            "Analyst II, Customer Insights & Analytics",
            RoleVerdict.STRONG_VARIANT,
        ),
        (
            "data_analyst",
            "Analyst, Customer Experience & Insights",
            RoleVerdict.STRONG_VARIANT,
        ),
        ("data_analyst", "Analyst, VBC Analytics", RoleVerdict.STRONG_VARIANT),
    ],
)
def test_a_comma_inverted_title_is_read_the_right_way_round(
    role_key: str, title: str, expected: RoleVerdict
) -> None:
    assessment = assess(role_key, title, seniority=Seniority.SENIOR)

    assert assessment.verdict is expected
    assert assessment.rejection is None
    # The explanation has to say what was done, or a reader cannot tell why a
    # title that plainly is not the role was accepted.
    assert "head-noun first" in assessment.reason


@pytest.mark.parametrize(
    "title",
    [
        # De-inverts to "Financial Crimes Lead Analyst" - a weak variant. An
        # anti-money-laundering job is not an FP&A job.
        "Lead Analyst, Financial Crimes",
        # De-inverts to "... Compliance Senior Analyst" - also weak.
        "Senior Analyst, National Medicaid Business Compliance",
    ],
)
def test_a_weak_variant_is_never_rescued(title: str) -> None:
    for role_key in ("financial_analyst", "business_analyst"):
        assessment = assess(role_key, title, seniority=Seniority.SENIOR)

        assert assessment.verdict is RoleVerdict.INCOMPATIBLE
        assert assessment.rejection is RoleRejection.ROLE_INCOMPATIBLE


def test_the_rescue_only_ever_returns_exact_or_strong() -> None:
    # Pinned as a property rather than a list of cases: whatever the rewrite
    # produces, the weak tier must never be how a title gets in.
    titles = [
        "Analyst, FP&A",
        "Lead Analyst, Financial Crimes",
        "Analyst, Revenue",
        "Analyst, Financial Systems",
        "Engineer, Financial Reporting",
    ]
    for title in titles:
        assessment = assess("financial_analyst", title, seniority=Seniority.SENIOR)

        assert assessment.verdict is not RoleVerdict.WEAK_VARIANT


@pytest.mark.parametrize(
    "title",
    [
        "Accountant, Corporate Finance",
        "Auditor, Internal",
        "Data Scientist, Risk",
        "Recruiter, Technical",
    ],
)
def test_a_comma_title_naming_another_profession_still_fails_the_foreign_check(
    title: str,
) -> None:
    # The foreign check decides at step 2 or 4, so the rescue is never reached
    # and a different profession cannot be de-inverted into this one.
    assessment = assess("financial_analyst", title)

    assert assessment.verdict is RoleVerdict.INCOMPATIBLE
    assert assessment.rejection is RoleRejection.ROLE_FAMILY_MISMATCH


def test_a_comma_title_does_not_escape_the_leadership_rule() -> None:
    # The rescue is reached only from the last check; a leadership refusal has
    # returned long before, and carries its own reason.
    assessment = assess(
        "data_analyst", "Principal Data Analyst, Card", seniority=Seniority.MID
    )

    assert assessment.verdict is RoleVerdict.INCOMPATIBLE
    assert assessment.rejection is RoleRejection.LEADERSHIP_MISMATCH


@pytest.mark.parametrize(
    "title",
    [
        # A list, not an inversion.
        "Analyst, Risk, EMEA",
        # Nothing on one side of the comma.
        "Analyst,",
        ", FP&A",
        # Too long to be this convention: swinging that clause in front of the
        # head noun could carry almost anything with it.
        "Analyst, Global Commercial Insights and Reporting Division",
        "Head of Data Science and Analytics, FP&A",
    ],
)
def test_arbitrary_comma_prose_is_not_treated_as_an_inversion(title: str) -> None:
    assessment = assess("financial_analyst", title, seniority=Seniority.SENIOR)

    assert assessment.verdict is RoleVerdict.INCOMPATIBLE


@pytest.mark.parametrize(
    ("role_key", "title", "expected"),
    [
        # Unchanged: these never reach the rescue.
        ("financial_analyst", "Financial Analyst", RoleVerdict.EXACT),
        ("financial_analyst", "Senior Financial Analyst", RoleVerdict.EXACT),
        (
            "financial_analyst",
            "Financial Analyst, Service Lines",
            RoleVerdict.STRONG_VARIANT,
        ),
        ("financial_analyst", "Financial Revenue Analyst", RoleVerdict.WEAK_VARIANT),
    ],
)
def test_titles_that_already_decided_are_untouched(
    role_key: str, title: str, expected: RoleVerdict
) -> None:
    assessment = assess(role_key, title, seniority=Seniority.SENIOR)

    assert assessment.verdict is expected
    assert "head-noun first" not in assessment.reason


def test_a_title_without_a_comma_cannot_reach_the_rescue() -> None:
    from app.matching.roles import _comma_inverted_title

    assert _comma_inverted_title("Financial Analyst") is None
    assert _comma_inverted_title("Senior Data Analyst") is None
