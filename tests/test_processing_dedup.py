"""Duplicate detection and the field-quality merge rules.

Two halves:

* **dedup** - is this the same vacancy? Hard evidence, probable evidence, and
  the cases that must stay separate.
* **merge** - when it is, which value survives? A new source may improve a
  field, never degrade it.

Cross-portal cases are built from constructed ``RawJob`` records rather than
live collection: capturing one vacancy on two portals at the same moment is not
something that can be committed as a fixture. These are constructed inputs, and
they are not evidence that any portal was contacted.

The second source uses an aggregator host that no adapter collects from. That
is deliberate - what matters to these rules is how ``app.processing.urls``
classifies the link, not which portal produced it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.constants import JobUrlKind, WorkMode
from app.portals.base import RawJob
from app.processing.dedup import (
    PROBABLE_DUPLICATE_WINDOW,
    DuplicateKind,
    ExistingJobView,
    best_match,
    compare,
)
from app.processing.locations import normalize_location
from app.processing.merge import (
    better_company,
    better_description,
    better_location,
    better_title,
    better_work_mode,
    earliest_posted_at,
    merge_into_existing,
    title_quality,
)
from app.processing.normalize import NormalizedJob, normalize_raw_job
from app.processing.titles import normalize_title

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


def make_raw(
    *,
    source: str = "jobright",
    external_id: str | None = "jr-1",
    title: str = "Financial Analyst",
    company: str | None = "Contoso",
    location: str | None = "Boston, MA",
    description: str | None = None,
    source_url: str = "https://jobright.ai/jobs/info/jr-1",
    application_url: str | None = None,
    posted_at: datetime | None = None,
    employment_type: str | None = "Full-time",
) -> RawJob:
    return RawJob(
        external_id=external_id,
        source=source,
        title=title,
        company=company,
        location=location,
        description=description,
        source_url=source_url,
        application_url=application_url or source_url,
        posted_at=posted_at if posted_at is not None else NOW - timedelta(days=1),
        employment_type=employment_type,
        raw_payload={},
    )


def normalize(**kwargs) -> NormalizedJob:
    result = normalize_raw_job(make_raw(**kwargs), now=NOW)
    assert isinstance(result, NormalizedJob), result
    return result


def as_view(job: NormalizedJob, *, job_id: str = "existing") -> ExistingJobView:
    """Treat a normalised listing as one already stored."""
    application_urls = frozenset(
        [job.application_url.canonical]
        if job.application_url
        and job.application_url.kind in (JobUrlKind.ATS, JobUrlKind.EMPLOYER_CAREER)
        else []
    )
    return ExistingJobView(
        job_id=job_id,
        canonical_key=job.canonical_key,
        title=job.title,
        company_key=job.company_key,
        company_display=job.company.display if job.company else None,
        location=job.location,
        posted_at=job.posted_at,
        description_fingerprint=job.description_fingerprint,
        source_canonical_urls=frozenset(job.candidate_canonical_urls),
        application_canonical_urls=application_urls,
        external_ids=frozenset(
            [(job.portal_slug, job.external_id)] if job.external_id else []
        ),
    )


# --- Hard duplicates --------------------------------------------------------


def test_the_same_portal_and_external_id_is_a_hard_duplicate() -> None:
    stored = normalize(external_id="jr-99")
    # Same listing rediscovered, with the employer's metadata since improved.
    incoming = normalize(external_id="jr-99", location="Cambridge, MA")

    verdict = compare(incoming, as_view(stored))

    assert verdict.kind is DuplicateKind.HARD
    assert verdict.confidence == 1.0
    assert any("external id" in signal for signal in verdict.signals)


def test_the_same_ats_application_url_is_a_hard_duplicate_across_portals() -> None:
    ats = "https://boards.greenhouse.io/contoso/jobs/4123456?gh_jid=4123456"
    stored = normalize(source="jobright", external_id="jr-1", application_url=ats)
    incoming = normalize(
        source="aggregator",
        external_id="li-1",
        source_url="https://www.indeed.com/viewjob?jk=3901234567",
        application_url=ats,
        # Different portals describe the same requisition differently.
        title="Financial Analyst - Corporate",
        location="Boston",
    )

    verdict = compare(incoming, as_view(stored))

    assert verdict.kind is DuplicateKind.HARD
    assert any("application URL" in signal for signal in verdict.signals)


def test_the_same_requisition_url_survives_tracking_differences() -> None:
    stored = normalize(
        application_url="https://boards.greenhouse.io/contoso/jobs/41?gh_jid=41"
    )
    incoming = normalize(
        source="aggregator",
        external_id="li-2",
        source_url="https://www.indeed.com/viewjob?jk=2",
        application_url=(
            "http://www.boards.greenhouse.io/contoso/jobs/41?gh_src=li&gh_jid=41"
        ),
    )

    assert compare(incoming, as_view(stored)).kind is DuplicateKind.HARD


def test_a_shared_portal_listing_url_is_not_hard_evidence() -> None:
    # Two aggregators can point at the same third-party listing without it
    # being the same requisition, so this must fall through to the gates.
    shared = "https://jobright.ai/jobs/info/shared"
    stored = normalize(external_id="a", source_url=shared, application_url=shared)
    incoming = normalize(
        external_id="b",
        source_url=shared,
        application_url=shared,
        company="Fabrikam",
    )

    verdict = compare(incoming, as_view(stored))

    assert verdict.kind is DuplicateKind.NONE


# --- Probable duplicates ----------------------------------------------------


def test_same_employer_role_and_place_is_a_probable_duplicate() -> None:
    stored = normalize(source="jobright", external_id="jr-1")
    incoming = normalize(
        source="aggregator",
        external_id="li-1",
        source_url="https://www.indeed.com/viewjob?jk=1",
    )

    verdict = compare(incoming, as_view(stored))

    assert verdict.kind is DuplicateKind.PROBABLE
    assert 0.0 < verdict.confidence <= 1.0


def test_a_less_specific_location_still_merges() -> None:
    stored = normalize(location="Boston, MA")
    incoming = normalize(
        source="aggregator",
        external_id="li-1",
        source_url="https://www.indeed.com/viewjob?jk=1",
        location="Boston",
    )

    assert compare(incoming, as_view(stored)).kind is DuplicateKind.PROBABLE


def test_identical_description_text_raises_confidence() -> None:
    body = "We are hiring a financial analyst to own the monthly close. " * 10
    stored = normalize(description=body)
    incoming = normalize(
        source="aggregator",
        external_id="li-1",
        source_url="https://www.indeed.com/viewjob?jk=1",
        description=body,
    )
    without_text = compare(
        normalize(
            source="aggregator",
            external_id="li-1",
            source_url="https://www.indeed.com/viewjob?jk=1",
        ),
        as_view(normalize()),
    )

    verdict = compare(incoming, as_view(stored))

    assert verdict.kind is DuplicateKind.PROBABLE
    assert verdict.confidence > without_text.confidence
    assert "identical description text" in verdict.signals


# --- Cases that must never merge -------------------------------------------


def test_different_employers_never_merge() -> None:
    stored = normalize(company="Contoso")
    incoming = normalize(company="Fabrikam", external_id="jr-2")

    verdict = compare(incoming, as_view(stored))

    assert verdict.kind is DuplicateKind.NONE
    assert verdict.blockers


def test_conflicting_locations_never_merge() -> None:
    stored = normalize(location="Boston, MA")
    incoming = normalize(location="Denver, CO", external_id="jr-2")

    verdict = compare(incoming, as_view(stored))

    assert verdict.kind is DuplicateKind.NONE
    assert any("conflict" in blocker for blocker in verdict.blockers)


def test_different_seniority_never_merges() -> None:
    stored = normalize(title="Senior Financial Analyst")
    incoming = normalize(title="Junior Financial Analyst", external_id="jr-2")

    verdict = compare(incoming, as_view(stored))

    assert verdict.kind is DuplicateKind.NONE
    assert any("seniority" in blocker for blocker in verdict.blockers)


def test_different_levels_never_merge() -> None:
    stored = normalize(title="Financial Analyst II")
    incoming = normalize(title="Financial Analyst III", external_id="jr-2")

    assert compare(incoming, as_view(stored)).kind is DuplicateKind.NONE


def test_different_roles_never_merge() -> None:
    stored = normalize(title="Financial Analyst")
    incoming = normalize(title="Software Engineer", external_id="jr-2")

    assert compare(incoming, as_view(stored)).kind is DuplicateKind.NONE


def test_postings_far_apart_in_time_are_separate_advertisements() -> None:
    stored = normalize(posted_at=NOW - PROBABLE_DUPLICATE_WINDOW - timedelta(days=10))
    incoming = normalize(external_id="jr-2", posted_at=NOW)

    verdict = compare(incoming, as_view(stored))

    assert verdict.kind is DuplicateKind.NONE
    assert any("window" in blocker for blocker in verdict.blockers)


def test_a_missing_employer_blocks_a_probable_merge() -> None:
    stored = normalize(company=None)
    incoming = normalize(company=None, external_id="jr-2")

    assert compare(incoming, as_view(stored)).kind is DuplicateKind.NONE


def test_an_unknown_posting_date_does_not_block_a_merge() -> None:
    stored = normalize(posted_at=None)
    incoming = normalize(
        source="aggregator",
        external_id="li-1",
        source_url="https://www.indeed.com/viewjob?jk=1",
    )

    assert compare(incoming, as_view(stored)).kind is DuplicateKind.PROBABLE


# --- Choosing between candidates -------------------------------------------


def test_hard_evidence_beats_probable_evidence() -> None:
    ats = "https://boards.greenhouse.io/contoso/jobs/1?gh_jid=1"
    probable = as_view(normalize(external_id="other"), job_id="probable")
    hard = as_view(
        normalize(external_id="exact", application_url=ats), job_id="hard"
    )
    incoming = normalize(
        source="aggregator",
        external_id="li-1",
        source_url="https://www.indeed.com/viewjob?jk=1",
        application_url=ats,
    )

    match, verdict = best_match(incoming, [probable, hard])

    assert match is not None and match.job_id == "hard"
    assert verdict.kind is DuplicateKind.HARD


def test_no_candidate_means_no_duplicate() -> None:
    match, verdict = best_match(normalize(), [])

    assert match is None
    assert verdict.is_duplicate is False


def test_the_same_candidates_always_produce_the_same_decision() -> None:
    # A non-deterministic merge would make processing unrepeatable.
    views = [
        as_view(normalize(external_id="a"), job_id="a"),
        as_view(normalize(external_id="b"), job_id="b"),
    ]
    incoming = normalize(
        source="aggregator",
        external_id="li-1",
        source_url="https://www.indeed.com/viewjob?jk=1",
    )

    first, _ = best_match(incoming, views)
    second, _ = best_match(incoming, list(reversed(views)))

    assert first is not None and second is not None
    assert first.job_id == second.job_id


def test_a_verdict_serialises_with_its_evidence() -> None:
    verdict = compare(
        normalize(
            source="aggregator",
            external_id="li-1",
            source_url="https://www.indeed.com/viewjob?jk=1",
        ),
        as_view(normalize()),
    )

    payload = verdict.as_dict()

    assert payload["kind"] == "probable"
    assert isinstance(payload["signals"], list) and payload["signals"]


# --- Field quality on merge -------------------------------------------------


def test_a_truncated_title_loses_to_a_complete_one() -> None:
    assert title_quality("Senior Financial Analyst...") < title_quality(
        "Senior Financial Analyst"
    )
    assert better_title("Senior Financial Analyst...", "Senior Financial Analyst") == (
        "Senior Financial Analyst"
    )


def test_a_shouted_title_loses_to_a_readable_one() -> None:
    assert better_title("SENIOR FINANCIAL ANALYST", "Senior Financial Analyst") == (
        "Senior Financial Analyst"
    )


def test_a_good_title_is_not_replaced_by_a_worse_one() -> None:
    assert better_title("Senior Financial Analyst", "SENIOR FINANCIAL ANALYST") == (
        "Senior Financial Analyst"
    )


def test_a_longer_description_wins_and_a_shorter_one_never_overwrites() -> None:
    full = "x" * 4000
    teaser = "y" * 200

    assert better_description(teaser, full) == full
    assert better_description(full, teaser) == full
    assert better_description(None, teaser) == teaser


def test_the_more_specific_location_wins_but_a_different_one_does_not() -> None:
    boston = normalize_location("Boston")
    boston_ma = normalize_location("Boston, MA")
    denver = normalize_location("Denver, CO")

    assert better_location(boston, boston_ma) is boston_ma
    assert better_location(boston_ma, boston) is boston_ma
    # Already judged compatible by this point; the stored value stays.
    assert better_location(boston_ma, denver) is boston_ma


def test_the_fuller_company_name_wins() -> None:
    assert better_company("Acme", "Acme Financial Services") == (
        "Acme Financial Services"
    )
    assert better_company("Acme Financial Services", "Acme") == (
        "Acme Financial Services"
    )


def test_the_earliest_posting_date_wins() -> None:
    early = NOW - timedelta(days=10)

    assert earliest_posted_at(NOW, early) == early
    assert earliest_posted_at(early, NOW) == early
    assert earliest_posted_at(None, NOW) == NOW


def test_a_stated_work_mode_is_never_overwritten() -> None:
    assert better_work_mode(WorkMode.REMOTE, WorkMode.ONSITE) is WorkMode.REMOTE
    assert better_work_mode(None, WorkMode.REMOTE) is WorkMode.REMOTE


def test_merging_the_same_source_twice_changes_nothing() -> None:
    incoming = normalize(description="d" * 500)

    outcome = merge_into_existing(
        existing_title=incoming.title.display,
        existing_company=incoming.company.display if incoming.company else None,
        existing_location=incoming.location,
        existing_work_mode=incoming.work_mode,
        existing_employment_type=incoming.employment_type,
        existing_description=incoming.description,
        existing_posted_at=incoming.posted_at,
        incoming=incoming,
    )

    assert outcome.has_changes is False


def test_a_merge_records_which_portal_supplied_each_new_value() -> None:
    incoming = normalize(source="aggregator", description="d" * 900)

    outcome = merge_into_existing(
        existing_title="Financial Analyst",
        existing_company="Contoso",
        existing_location=normalize_location("Boston, MA"),
        existing_work_mode=None,
        existing_employment_type=None,
        existing_description="short",
        existing_posted_at=None,
        incoming=incoming,
    )

    assert "description" in outcome.changed
    assert outcome.provenance["description"] == "aggregator"


def test_a_new_source_cannot_degrade_a_stored_field() -> None:
    incoming = normalize(title="FINANCIAL ANALYST", description="tiny")

    outcome = merge_into_existing(
        existing_title="Financial Analyst",
        existing_company="Contoso Corporation",
        existing_location=normalize_location("Boston, MA"),
        existing_work_mode=WorkMode.HYBRID,
        existing_employment_type="Full-time",
        existing_description="d" * 5000,
        existing_posted_at=NOW - timedelta(days=5),
        incoming=incoming,
    )

    assert outcome.changed == {}


@pytest.mark.parametrize(
    "title",
    ["Financial Analyst", "Sr. Financial Analyst (Remote)", "FINANCIAL ANALYST II"],
)
def test_normalising_is_stable_under_repetition(title: str) -> None:
    first = normalize(title=title)
    second = normalize(title=title)

    assert first.canonical_key == second.canonical_key
    assert first.title.key == second.title.key


def test_normalised_titles_used_for_comparison_are_recoverable() -> None:
    # The service re-derives comparison values from stored display text, so a
    # round trip through the display value must produce the same key.
    job = normalize(title="Sr. Financial Analyst (Remote) - Req #48210")
    recovered = normalize_title(job.title.display)

    assert recovered is not None
    assert recovered.key == job.title.key
