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


# --- Slash-joined titles ----------------------------------------------------
#
# "AI/ML Engineer" names one job two ways and shares the head noun between
# them. Every title here was captured from a live posting during the recall
# audit, except the deliberately hostile ones marked as such.


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("AI/ML Engineer", RoleVerdict.EXACT),
        ("ML/AI Engineer", RoleVerdict.EXACT),
        ("AI/Machine Learning Engineer", RoleVerdict.EXACT),
        ("Senior AI/ML Engineer", RoleVerdict.EXACT),
        ("ML/AI Engineer II", RoleVerdict.EXACT),
        ("Azure AI/ML Engineer", RoleVerdict.STRONG_VARIANT),
        ("ML/AI Engineer - Consultant", RoleVerdict.STRONG_VARIANT),
    ],
)
def test_a_slash_joined_title_is_read_as_each_role_it_names(
    title: str, expected: RoleVerdict
) -> None:
    assessment = assess(
        "machine_learning_engineer", title, seniority=Seniority.SENIOR
    )

    assert assessment.verdict is expected
    assert assessment.rejection is None
    assert "side of the slash" in assessment.reason


def test_a_slash_title_keeps_its_numeric_level_handling() -> None:
    # "II" is stripped by normalisation exactly as it is without a slash, so
    # the level marker changes nothing about the verdict.
    with_level = assess(
        "machine_learning_engineer", "ML/AI Engineer II", seniority=Seniority.SENIOR
    )
    without = assess(
        "machine_learning_engineer", "ML/AI Engineer", seniority=Seniority.SENIOR
    )

    assert with_level.verdict is without.verdict is RoleVerdict.EXACT


def test_a_slash_title_does_not_escape_the_leadership_rule() -> None:
    assessment = assess(
        "data_analyst", "Principal Data Analyst/Lead", seniority=Seniority.MID
    )

    assert assessment.verdict is RoleVerdict.INCOMPATIBLE
    assert assessment.rejection is RoleRejection.LEADERSHIP_MISMATCH


def test_a_slash_title_naming_another_profession_is_refused_outright() -> None:
    # The hostile case. One reading is the target role EXACTLY, the other is a
    # different career. Taking the flattering half is what must not happen.
    assessment = assess("financial_analyst", "Financial Analyst/Accountant")

    assert assessment.verdict is RoleVerdict.INCOMPATIBLE
    assert "Accountant" not in assessment.reason or assessment.rejection is not None


@pytest.mark.parametrize(
    ("role_key", "title"),
    [
        # Both sides land outside the target role.
        ("financial_analyst", "Analyst/Consultant"),
        ("machine_learning_engineer", "Research/Applied Scientist"),
        # More than one slash: not this convention, left alone.
        ("machine_learning_engineer", "Data/ML/AI Engineer"),
        # Non-alphabetic sides.
        ("machine_learning_engineer", "24/7 Support Engineer"),
    ],
)
def test_a_slash_title_the_gate_does_not_support_stays_rejected(
    role_key: str, title: str
) -> None:
    assessment = assess(role_key, title, seniority=Seniority.SENIOR)

    assert assessment.verdict is RoleVerdict.INCOMPATIBLE


def test_the_slash_rescue_never_returns_a_weak_variant() -> None:
    titles = [
        "AI/ML Engineer",
        "Financial/Revenue Analyst",
        "Data/Reporting Analyst",
        "Network/Wireless Engineer",
    ]
    for role_key, title in (
        ("machine_learning_engineer", titles[0]),
        ("financial_analyst", titles[1]),
        ("data_analyst", titles[2]),
        ("network_engineer", titles[3]),
    ):
        assessment = assess(role_key, title, seniority=Seniority.SENIOR)

        assert assessment.verdict is not RoleVerdict.WEAK_VARIANT


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("AI/ML Engineer", ("AI Engineer", "ML Engineer")),
        ("ML/AI Engineer II", ("ML Engineer II", "AI Engineer II")),
        ("Senior AI/ML Engineer", ("Senior AI Engineer", "Senior ML Engineer")),
        # Only the slashed word is split; the shared head stays put.
        (
            "AI/Machine Learning Engineer",
            ("AI Learning Engineer", "Machine Learning Engineer"),
        ),
        # Spaced separator is the same convention.
        ("Network Engineer / Architect", ("Network Engineer", "Network Architect")),
        # Refused: several slashes, no shared head, or non-alphabetic sides.
        ("Data/ML/AI Engineer", ()),
        ("Analyst/Consultant", ()),
        ("24/7 Support Engineer", ()),
        ("Engineer (1/2 time)", ()),
        ("Financial Analyst", ()),
    ],
)
def test_slash_candidates_are_generated_conservatively(
    title: str, expected: tuple[str, ...]
) -> None:
    from app.matching.roles import _slash_title_candidates

    assert _slash_title_candidates(title) == expected


def test_a_title_with_both_punctuation_marks_has_one_predictable_answer() -> None:
    # Comma is tried first, then slash. Running it twice must give the same
    # answer, and nothing may recurse.
    first = assess("data_analyst", "Analyst, Risk/EMEA", seniority=Seniority.SENIOR)
    second = assess("data_analyst", "Analyst, Risk/EMEA", seniority=Seniority.SENIOR)

    assert first.verdict is second.verdict
    assert first.reason == second.reason


@pytest.mark.parametrize(
    ("role_key", "title", "expected"),
    [
        # F4 must be untouched by F3.
        ("financial_analyst", "Analyst, FP&A", RoleVerdict.EXACT),
        ("machine_learning_engineer", "Engineer, Machine Learning", RoleVerdict.EXACT),
        ("financial_analyst", "Lead Analyst, Financial Crimes", RoleVerdict.INCOMPATIBLE),
        # And the ordinary paths too.
        ("financial_analyst", "Financial Analyst", RoleVerdict.EXACT),
        ("financial_analyst", "Financial Revenue Analyst", RoleVerdict.WEAK_VARIANT),
        ("financial_analyst", "Accountant", RoleVerdict.INCOMPATIBLE),
    ],
)
def test_titles_without_a_slash_are_unaffected(
    role_key: str, title: str, expected: RoleVerdict
) -> None:
    assessment = assess(role_key, title, seniority=Seniority.SENIOR)

    assert assessment.verdict is expected
    assert "side of the slash" not in assessment.reason


def test_slash_bearing_skills_are_still_single_skills() -> None:
    # The whole reason the rescue is title-only: `normalize_token` keeps "/"
    # so these stay one skill each, and nothing here may change that.
    from app.matching.requirements import extract_requirements as extract

    job = extract(
        title="Network Engineer",
        description=(
            "Requirements:\n- Must have TCP/IP and CI/CD experience\n"
            "- LAN/WAN design\n" + DESCRIPTION
        ),
    )

    keys = {skill.skill_key for skill in job.skills}
    assert "tcp/ip" in keys
    assert "ci/cd" in keys
    # Split halves must never appear as skills of their own.
    assert not {"tcp", "ip", "ci", "cd", "lan", "wan"} & keys


def test_no_rewrite_is_ever_applied_to_another_rewrite() -> None:
    # The rescues call `_assess` directly, never `evaluate_role`, so a rewritten
    # title cannot itself be rewritten. Counted rather than argued: one base
    # assessment, at most one comma candidate and at most two slash candidates.
    import app.matching.roles as roles_module

    calls: list[str] = []
    original = roles_module._assess

    def counting(profile, job):
        calls.append(job.title)
        return original(profile, job)

    monkeypatched = pytest.MonkeyPatch()
    monkeypatched.setattr(roles_module, "_assess", counting)
    try:
        for title in (
            "Analyst, Risk/EMEA",
            "AI/ML Engineer",
            "Analyst, FP&A",
            "Financial Analyst",
        ):
            calls.clear()
            assess("machine_learning_engineer", title, seniority=Seniority.SENIOR)

            assert len(calls) <= 4, (title, calls)
            assert calls[0] == title
            # No candidate may reappear as the input to a later rewrite.
            assert len(calls) == len(set(calls)), calls
    finally:
        monkeypatched.undo()


# --- A target role named by qualifying another role -------------------------
#
# hr_analyst lists "HR Data Analyst" and "HR Reporting Analyst" among its own
# approved phrases, and both contain a data_analyst title. The foreign check
# must not read the part and miss the whole.


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        # Resolve exactly through the taxonomy - the path was always open.
        ("HR Data Analyst", RoleVerdict.EXACT),
        ("Senior HR Data Analyst", RoleVerdict.EXACT),
        ("HR Data Analyst I", RoleVerdict.EXACT),
        ("Human Resources Data Analyst", RoleVerdict.EXACT),
        # Qualified forms, which used to be refused as Data Analyst.
        ("AOC HR Data Analyst", RoleVerdict.STRONG_VARIANT),
        ("HR Data Analyst - HRMS", RoleVerdict.STRONG_VARIANT),
        ("HR Reporting Analyst - Global Payroll", RoleVerdict.STRONG_VARIANT),
    ],
)
def test_the_target_roles_own_longer_phrase_wins(
    title: str, expected: RoleVerdict
) -> None:
    assessment = assess("hr_analyst", title)

    assert assessment.verdict is expected
    assert assessment.rejection is None


@pytest.mark.parametrize(
    "title",
    [
        # No approved HR phrase appears in any of these, so the foreign name
        # is the only role either of them states.
        "Data Analyst",
        "Senior Data Analyst",
        "Financial Data Analyst",
        "Business Data Analyst",
        "Marketing Data Analyst",
        "Data Analyst - Workforce Reporting",
        # A different HR role, not a qualified HR Analyst.
        "HR Business Partner",
        # "HR Data Engineer" is not in the taxonomy as an HR Analyst phrase.
        "HR Data Engineer",
    ],
)
def test_a_foreign_role_not_inside_an_approved_phrase_is_still_refused(
    title: str,
) -> None:
    assessment = assess("hr_analyst", title)

    assert assessment.verdict is RoleVerdict.INCOMPATIBLE
    assert assessment.rejection is RoleRejection.ROLE_FAMILY_MISMATCH


def test_the_precedence_does_not_run_in_the_other_direction() -> None:
    # "HR Data Analyst" contains "Data Analyst", but a Data Analyst resume has
    # no approved phrase containing "HR Analyst", so nothing dominates and the
    # posting stays refused.
    assessment = assess("data_analyst", "HR Data Analyst")

    assert assessment.verdict is RoleVerdict.INCOMPATIBLE
    assert assessment.rejection is RoleRejection.ROLE_FAMILY_MISMATCH


def test_a_held_foreign_rejection_outranks_the_weak_tier() -> None:
    # The containment rule opens the door only as far as the strong tier. A
    # title that would merely be a weak variant keeps its foreign rejection.
    from app.matching.roles import _assess

    job = extract_requirements(
        title="HR Analytics Data Analyst", description=DESCRIPTION
    )
    profile = profile_for("hr_analyst")
    assessment = _assess(profile, job)

    assert assessment.verdict is not RoleVerdict.WEAK_VARIANT
    if assessment.verdict is RoleVerdict.INCOMPATIBLE:
        assert assessment.rejection is RoleRejection.ROLE_FAMILY_MISMATCH


def test_the_dominating_phrase_must_be_longer_and_actually_present() -> None:
    from app.matching.roles import _dominating_target_phrase

    profile = profile_for("hr_analyst")
    spec = taxonomy.get_role("hr_analyst")

    # Present and longer than "data analyst" - dominates.
    assert (
        _dominating_target_phrase(
            "aoc hr data analyst", "data_analyst", spec, profile
        )
        == "hr data analyst"
    )
    # The approved phrase is not in this title at all.
    assert (
        _dominating_target_phrase(
            "financial data analyst", "data_analyst", spec, profile
        )
        is None
    )
    # Nothing dominates a foreign role the target never qualifies.
    assert (
        _dominating_target_phrase(
            "hr business partner", "hr_business_partner", spec, profile
        )
        is None
    )


def test_leadership_still_decides_before_any_of_this() -> None:
    assessment = assess(
        "hr_analyst", "Director, HR Data Analyst", seniority=Seniority.MID
    )

    assert assessment.verdict is RoleVerdict.INCOMPATIBLE
    assert assessment.rejection is RoleRejection.LEADERSHIP_MISMATCH


@pytest.mark.parametrize(
    ("role_key", "title", "expected"),
    [
        # F4 comma inversion.
        ("financial_analyst", "Analyst, FP&A", RoleVerdict.EXACT),
        ("machine_learning_engineer", "Engineer, Machine Learning", RoleVerdict.EXACT),
        (
            "financial_analyst",
            "Lead Analyst, Financial Crimes",
            RoleVerdict.INCOMPATIBLE,
        ),
        # F3 slash rescue.
        ("machine_learning_engineer", "AI/ML Engineer", RoleVerdict.EXACT),
        ("machine_learning_engineer", "Azure AI/ML Engineer", RoleVerdict.STRONG_VARIANT),
        ("financial_analyst", "Financial Analyst/Accountant", RoleVerdict.INCOMPATIBLE),
        # Ordinary paths.
        ("financial_analyst", "Financial Analyst", RoleVerdict.EXACT),
        ("financial_analyst", "Financial Analyst, Service Lines", RoleVerdict.STRONG_VARIANT),
        ("financial_analyst", "Financial Revenue Analyst", RoleVerdict.WEAK_VARIANT),
        ("financial_analyst", "Accountant", RoleVerdict.INCOMPATIBLE),
        ("financial_analyst", "Staff Accountant", RoleVerdict.INCOMPATIBLE),
    ],
)
def test_the_other_rescues_and_the_ordinary_paths_are_unchanged(
    role_key: str, title: str, expected: RoleVerdict
) -> None:
    assessment = assess(role_key, title, seniority=Seniority.SENIOR)

    assert assessment.verdict is expected
