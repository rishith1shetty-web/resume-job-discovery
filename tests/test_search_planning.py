"""Query planning, freshness windows and allocation.

Pure functions over plain values - no database, no portals, no network. These
are the rules the orchestration depends on, so they are asserted directly
rather than inferred from an end-to-end run.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.constants import FRESHNESS_ESCALATION, FreshnessWindow, Seniority
from app.matching.profile import ResumeMatchProfile
from app.matching.roles import RoleVerdict
from app.search.allocation import Candidate, allocate, order_candidates
from app.search.freshness import (
    freshness_score,
    windows_from,
    within_window,
)
from app.search.queries import DEFAULT_MAX_QUERIES_PER_PORTAL, build_query_plan
from app.services.quota import compute_quotas

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


def profile_for(
    role: str = "financial_analyst",
    *,
    seniority: Seniority | None = Seniority.MID,
    domain: str | None = "finance",
) -> ResumeMatchProfile:
    return ResumeMatchProfile(
        resume_id=uuid.uuid4(),
        target_role_key=role,
        target_role_display=role.replace("_", " ").title(),
        domain_key=domain,
        seniority=seniority,
        years_experience=5.0,
        skill_keys=frozenset({"excel", "sql"}),
        skill_display={},
        role_variation_keys=frozenset(),
        education=(),
        certifications=(),
        responsibilities=(),
    )


# --- Query generation -------------------------------------------------------


def test_the_plan_leads_with_the_canonical_title() -> None:
    plan = build_query_plan(profile_for())

    assert plan.queries[0] == "Financial Analyst"
    assert plan.role_key == "financial_analyst"


def test_the_same_profile_always_produces_the_same_queries() -> None:
    first = build_query_plan(profile_for())
    second = build_query_plan(profile_for())

    assert first.queries == second.queries


def test_the_plan_holds_only_same_role_variations() -> None:
    # CLAUDE.md section 3: a Financial Analyst resume must never be searched
    # as an adjacent finance career.
    plan = build_query_plan(profile_for())
    lowered = [query.lower() for query in plan.queries]

    forbidden = (
        "accountant",
        "auditor",
        "business analyst",
        "data analyst",
        "financial advisor",
        "investment banker",
        "finance manager",
        "sales analyst",
        "tax consultant",
    )
    for term in forbidden:
        assert not any(term in query for query in lowered), (term, lowered)


def test_a_data_analyst_is_not_broadened_into_engineering_or_science() -> None:
    plan = build_query_plan(profile_for("data_analyst"))
    lowered = [query.lower() for query in plan.queries]

    assert not any("data engineer" in query for query in lowered), lowered
    assert not any("data scientist" in query for query in lowered), lowered


def test_duplicate_titles_are_removed() -> None:
    plan = build_query_plan(profile_for())
    normalised = [query.lower().strip() for query in plan.queries]

    assert len(normalised) == len(set(normalised))


def test_the_query_count_is_capped() -> None:
    plan = build_query_plan(profile_for(), max_queries=3)

    assert len(plan.queries) <= 3


def test_the_default_cap_applies_without_an_argument() -> None:
    plan = build_query_plan(profile_for())

    assert len(plan.queries) <= DEFAULT_MAX_QUERIES_PER_PORTAL


def test_seniority_adds_a_prefixed_title_when_there_is_room() -> None:
    # The role's own same-role phrasings come first and are never crowded out
    # by a prefixed form, so a seniority variant only appears once the base
    # titles are exhausted. At the default cap the two plans coincide, which is
    # fine: the unprefixed title is the one that matches a portal's taxonomy.
    senior = build_query_plan(profile_for(seniority=Seniority.SENIOR), max_queries=10)
    mid = build_query_plan(profile_for(seniority=Seniority.MID), max_queries=10)

    assert any(query.startswith("Senior ") for query in senior.queries)
    assert not any(query.startswith("Senior ") for query in mid.queries)
    assert senior.queries[0] == mid.queries[0] == "Financial Analyst"


def test_a_profile_with_no_role_produces_no_queries() -> None:
    # Searching with no role would put every job through an open gate.
    plan = build_query_plan(profile_for(role=""))

    assert plan.queries == ()
    assert plan.is_empty


def test_an_unknown_role_produces_no_queries() -> None:
    plan = build_query_plan(profile_for(role="not_a_real_role"))

    assert plan.is_empty


def test_max_queries_must_be_positive() -> None:
    with pytest.raises(ValueError):
        build_query_plan(profile_for(), max_queries=0)


# --- Freshness --------------------------------------------------------------


@pytest.mark.parametrize(
    ("age_hours", "window", "expected"),
    [
        (2, FreshnessWindow.LAST_24H, True),
        (30, FreshnessWindow.LAST_24H, False),
        (30, FreshnessWindow.LAST_3D, True),
        (24 * 5, FreshnessWindow.LAST_3D, False),
        (24 * 5, FreshnessWindow.LAST_7D, True),
        (24 * 10, FreshnessWindow.LAST_14D, True),
        (24 * 20, FreshnessWindow.LAST_30D, True),
        (24 * 200, FreshnessWindow.LAST_30D, False),
        (24 * 200, FreshnessWindow.OLDER_ACTIVE, True),
    ],
)
def test_window_membership(
    age_hours: int, window: FreshnessWindow, expected: bool
) -> None:
    posted = NOW - timedelta(hours=age_hours)

    assert within_window(posted, window, NOW) is expected


def test_an_undated_listing_waits_for_the_final_window() -> None:
    # CLAUDE.md section 6: a posting date is never invented. An undated but
    # active listing is still a real result, so it is admitted last rather than
    # discarded or guessed into a fresher window.
    for window in FRESHNESS_ESCALATION[:-1]:
        assert within_window(None, window, NOW) is False

    assert within_window(None, FreshnessWindow.OLDER_ACTIVE, NOW) is True


def test_a_future_posting_counts_as_current() -> None:
    assert within_window(NOW + timedelta(hours=2), FreshnessWindow.LAST_24H, NOW)


def test_freshness_decays_with_age() -> None:
    fresh = freshness_score(NOW, NOW)
    week_old = freshness_score(NOW - timedelta(days=7), NOW)
    ancient = freshness_score(NOW - timedelta(days=90), NOW)

    assert fresh == 100.0
    assert 0.0 < week_old < fresh
    assert ancient == 0.0


def test_an_undated_listing_is_not_given_a_flattering_freshness() -> None:
    assert freshness_score(None, NOW) == 0.0


def test_the_escalation_ladder_is_newest_first() -> None:
    assert windows_from(FreshnessWindow.LAST_7D) == (
        FreshnessWindow.LAST_24H,
        FreshnessWindow.LAST_3D,
        FreshnessWindow.LAST_7D,
    )


# --- Allocation -------------------------------------------------------------


def candidate(
    *,
    portal: int = 0,
    score: float = 80.0,
    age_days: float = 1.0,
    verdict: RoleVerdict = RoleVerdict.EXACT,
    coverage: float = 1.0,
    key: str | None = None,
    job_id: uuid.UUID | None = None,
) -> Candidate:
    return Candidate(
        job_id=job_id or uuid.uuid4(),
        portal_index=portal,
        final_score=score,
        posted_at=NOW - timedelta(days=age_days),
        role_verdict=verdict,
        skill_coverage=coverage,
        canonical_key=key or uuid.uuid4().hex,
    )


def test_two_portals_share_twenty_evenly() -> None:
    candidates = [candidate(portal=0) for _ in range(15)] + [
        candidate(portal=1) for _ in range(15)
    ]

    result = allocate(candidates, quotas=compute_quotas(20, 2), requested_limit=20)

    assert result.per_portal_selected == (10, 10)
    assert result.returned_count == 20


def test_four_portals_share_fifty_within_one() -> None:
    candidates = [
        candidate(portal=index) for index in range(4) for _ in range(20)
    ]

    result = allocate(candidates, quotas=compute_quotas(50, 4), requested_limit=50)

    assert sorted(result.per_portal_selected) == [12, 12, 13, 13]
    assert result.returned_count == 50


def test_eight_portals_share_one_hundred_within_one() -> None:
    candidates = [
        candidate(portal=index) for index in range(8) for _ in range(20)
    ]

    result = allocate(candidates, quotas=compute_quotas(100, 8), requested_limit=100)

    assert set(result.per_portal_selected) == {12, 13}
    assert result.returned_count == 100


def test_a_short_portal_releases_its_slots_to_the_others() -> None:
    # Portal 0 has only 3 qualified jobs against a quota of 10.
    candidates = [candidate(portal=0) for _ in range(3)] + [
        candidate(portal=1) for _ in range(20)
    ]

    result = allocate(candidates, quotas=compute_quotas(20, 2), requested_limit=20)

    assert result.per_portal_selected == (3, 17)
    assert result.shortfalls == (7, 0)
    assert result.redistributed == 7
    assert result.returned_count == 20


def test_a_shortage_nobody_can_cover_returns_fewer_results() -> None:
    # Both portals are short. Nothing invents a job to close the gap.
    candidates = [candidate(portal=0) for _ in range(4)] + [
        candidate(portal=1) for _ in range(3)
    ]

    result = allocate(candidates, quotas=compute_quotas(20, 2), requested_limit=20)

    assert result.returned_count == 7
    assert result.total_shortfall == 13


def test_one_portal_may_fill_the_whole_request_when_it_is_the_only_one() -> None:
    candidates = [candidate(portal=0) for _ in range(40)]

    result = allocate(candidates, quotas=compute_quotas(20, 1), requested_limit=20)

    assert result.per_portal_selected == (20,)
    assert result.returned_count == 20


def test_the_total_never_exceeds_the_requested_limit() -> None:
    candidates = [
        candidate(portal=index) for index in range(2) for _ in range(500)
    ]

    result = allocate(candidates, quotas=compute_quotas(20, 2), requested_limit=20)

    assert result.returned_count == 20


def test_results_are_ordered_by_score_first() -> None:
    weak = candidate(score=71.0, age_days=0.0)
    strong = candidate(score=94.0, age_days=7.0)

    ordered = order_candidates([weak, strong])

    # A week-old 94% outranks a fresh 71%: freshness prioritises, it does not
    # override relevance (CLAUDE.md section 8).
    assert ordered == [strong, weak]


def test_recency_breaks_a_score_tie() -> None:
    older = candidate(score=80.0, age_days=9.0)
    newer = candidate(score=80.0, age_days=1.0)

    assert order_candidates([older, newer]) == [newer, older]


def test_role_strength_breaks_a_score_and_date_tie() -> None:
    variant = candidate(score=80.0, age_days=1.0, verdict=RoleVerdict.WEAK_VARIANT)
    exact = candidate(score=80.0, age_days=1.0, verdict=RoleVerdict.EXACT)

    assert order_candidates([variant, exact]) == [exact, variant]


def test_skill_coverage_breaks_the_next_tie() -> None:
    thin = candidate(score=80.0, age_days=1.0, coverage=0.4)
    thorough = candidate(score=80.0, age_days=1.0, coverage=0.95)

    assert order_candidates([thin, thorough]) == [thorough, thin]


def test_the_canonical_identifier_is_the_final_tie_breaker() -> None:
    first = candidate(score=80.0, age_days=1.0, key="aaa")
    second = candidate(score=80.0, age_days=1.0, key="bbb")

    assert order_candidates([second, first]) == [first, second]


def test_ordering_does_not_depend_on_input_order() -> None:
    pool = [
        candidate(score=score, age_days=age, key=key)
        for score, age, key in (
            (91.0, 3.0, "c"),
            (78.0, 1.0, "a"),
            (91.0, 1.0, "b"),
            (78.0, 5.0, "d"),
        )
    ]

    forwards = [item.job_id for item in order_candidates(pool)]
    backwards = [item.job_id for item in order_candidates(list(reversed(pool)))]

    assert forwards == backwards


def test_an_undated_job_sorts_after_dated_ones_of_the_same_score() -> None:
    dated = candidate(score=80.0, age_days=20.0)
    undated = Candidate(
        job_id=uuid.uuid4(),
        portal_index=0,
        final_score=80.0,
        posted_at=None,
        role_verdict=RoleVerdict.EXACT,
        skill_coverage=1.0,
        canonical_key="aaa",
    )

    assert order_candidates([undated, dated]) == [dated, undated]


def test_a_higher_scoring_undated_job_still_wins() -> None:
    dated = candidate(score=80.0, age_days=0.0)
    undated = Candidate(
        job_id=uuid.uuid4(),
        portal_index=0,
        final_score=95.0,
        posted_at=None,
        role_verdict=RoleVerdict.EXACT,
        skill_coverage=1.0,
        canonical_key="zzz",
    )

    assert order_candidates([undated, dated]) == [undated, dated]


def test_allocation_needs_at_least_one_portal() -> None:
    with pytest.raises(ValueError):
        allocate([], quotas=[], requested_limit=20)


def test_no_candidates_returns_an_empty_allocation() -> None:
    result = allocate([], quotas=compute_quotas(20, 2), requested_limit=20)

    assert result.returned_count == 0
    assert result.shortfalls == (10, 10)
