"""The universal application-type filter (Phase 13).

Only jobs that ask for a real application are returned. Quick Apply, Easy Apply
and one-click listings are refused before matching, whichever portal carried
them.

Every portal case here is driven from the **captured fixtures**, not from
hand-written records, so a test that says "SimplyHired publishes 44 Quick Apply
cards out of 60" is reporting what that portal actually returned rather than
what this phase wishes it returned.

Nothing in this file makes a network request.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.core.constants import ApplicationType
from app.portals.adapters.greenhouse import GreenhousePortal
from app.portals.adapters.hiringcafe import HiringCafePortal
from app.portals.adapters.jobright import JobrightPortal
from app.portals.adapters.scalejobs import ScaleJobsPortal
from app.portals.adapters.simplyhired import SimplyHiredPortal
from app.portals.base import (
    APPLICATION_TYPE_KEY,
    RawJob,
    declare_application_type,
)
from app.processing.application_type import (
    DESCRIPTION_SCAN_CHARS,
    QUICK_APPLY_REJECTION,
    classify_application_type,
    declared_application_type,
)
from app.processing.normalize import RejectedJob, RejectionReason, normalize_raw_job

FIXTURES = Path(__file__).parent / "fixtures" / "portals"

LONG_DESCRIPTION = (
    "We are hiring a Financial Analyst to own forecasting and the monthly "
    "close.\n\nRequirements:\n- 3+ years of financial analysis\n- Advanced "
    "Excel\n- SQL\n\nThe role reports into the FP&A lead and partners with "
    "business units on planning and reporting."
)


def make_raw(**overrides: Any) -> RawJob:
    """One collected listing, long-application by default."""
    payload = overrides.pop("raw_payload", {})
    fields: dict[str, Any] = {
        "external_id": "job-1",
        "source": "testportal",
        "title": "Financial Analyst",
        "company": "Northwind",
        "location": "Boston, MA",
        "description": LONG_DESCRIPTION,
        "source_url": "https://boards.greenhouse.io/northwind/jobs/1",
        "application_url": "https://boards.greenhouse.io/northwind/jobs/1",
        "posted_at": None,
        "employment_type": "Full-time",
        "raw_payload": payload,
    }
    fields.update(overrides)
    return RawJob(**fields)


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def walk_for(payload: Any, key: str) -> list[dict[str, Any]]:
    """Every dict in a payload carrying ``key``."""
    found: list[dict[str, Any]] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            if key in node:
                found.append(node)
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(payload)
    return found


# ---------------------------------------------------------------------------
# The vocabulary
# ---------------------------------------------------------------------------


class TestApplicationType:
    def test_only_quick_apply_is_ineligible(self) -> None:
        assert ApplicationType.LONG_APPLICATION.is_eligible is True
        assert ApplicationType.UNKNOWN.is_eligible is True
        assert ApplicationType.QUICK_APPLY.is_eligible is False

    def test_the_three_states_are_exactly_the_specified_ones(self) -> None:
        assert {t.value for t in ApplicationType} == {
            "long_application",
            "quick_apply",
            "unknown",
        }


class TestDeclaration:
    def test_a_portal_declaration_round_trips(self) -> None:
        payload = declare_application_type({}, ApplicationType.QUICK_APPLY)

        assert payload[APPLICATION_TYPE_KEY] == "quick_apply"
        assert (
            declared_application_type(make_raw(raw_payload=payload))
            is ApplicationType.QUICK_APPLY
        )

    def test_no_declaration_is_not_an_error(self) -> None:
        assert declared_application_type(make_raw()) is None

    def test_an_unrecognised_declaration_is_ignored_not_fatal(self) -> None:
        # A portal shipping a typo should cost precision, not the whole batch.
        raw = make_raw(raw_payload={APPLICATION_TYPE_KEY: "sort-of-easy"})

        assert declared_application_type(raw) is None
        # And the listing is still classified on evidence rather than dropped.
        assert classify_application_type(raw).is_eligible


# ---------------------------------------------------------------------------
# The classifier
# ---------------------------------------------------------------------------


class TestClassifierPrecedence:
    def test_a_portal_declaration_wins(self) -> None:
        raw = make_raw(
            raw_payload=declare_application_type({}, ApplicationType.QUICK_APPLY)
        )
        verdict = classify_application_type(raw)

        assert verdict.application_type is ApplicationType.QUICK_APPLY
        assert verdict.is_quick_apply

    def test_a_declared_long_application_is_accepted(self) -> None:
        raw = make_raw(
            raw_payload=declare_application_type(
                {}, ApplicationType.LONG_APPLICATION
            )
        )

        assert classify_application_type(raw).is_eligible

    def test_a_declared_unknown_still_falls_through_to_evidence(self) -> None:
        """UNKNOWN means "I cannot tell", not "stop looking"."""
        raw = make_raw(
            title="Financial Analyst - Easy Apply",
            raw_payload=declare_application_type({}, ApplicationType.UNKNOWN),
        )

        assert classify_application_type(raw).is_quick_apply


class TestClassifierEvidence:
    @pytest.mark.parametrize(
        "url",
        [
            "https://apply.indeed.com/indeedapply/form/resume",
            "https://smartapply.indeed.com/beta/indeedapply/form",
            "https://my.indeed.com/resume-apply",
            "https://easyapply.co/apply/9",
        ],
    )
    def test_a_one_click_destination_is_quick_apply(self, url: str) -> None:
        verdict = classify_application_type(make_raw(application_url=url))

        assert verdict.is_quick_apply
        assert verdict.evidence

    @pytest.mark.parametrize(
        "url",
        [
            "https://careers.acme.com/jobs/easyapply/9",
            "https://jobs.acme.com/one-click-apply/9",
        ],
    )
    def test_a_one_click_path_is_quick_apply(self, url: str) -> None:
        assert classify_application_type(make_raw(application_url=url)).is_quick_apply

    @pytest.mark.parametrize(
        "url",
        [
            "https://boards.greenhouse.io/stripe/jobs/1",
            "https://acme.wd1.myworkdayjobs.com/careers/job/1",
            "https://careers-acme.icims.com/jobs/8263/job",
            "https://acme.com/careers/financial-analyst",
        ],
    )
    def test_an_employer_or_ats_destination_is_not_quick_apply(
        self, url: str
    ) -> None:
        verdict = classify_application_type(make_raw(application_url=url))

        assert verdict.is_eligible
        assert verdict.application_type is ApplicationType.UNKNOWN

    def test_where_a_job_was_listed_is_not_where_it_is_applied_to(self) -> None:
        """Regression, caught by the existing deduplication tests.

        A job found on a big job site but applied for on the employer's ATS is
        the normal case. Judging it by the site it was listed on refused a real
        Greenhouse application - and would have refused most of the market.
        """
        raw = make_raw(
            source_url="https://www.linkedin.com/jobs/view/1",
            application_url="https://boards.greenhouse.io/contoso/jobs/1",
        )

        assert classify_application_type(raw).is_eligible

    def test_a_listing_host_is_not_treated_as_an_apply_form(self) -> None:
        # Whether a LinkedIn posting is one-click is a property of the posting,
        # not of the domain, so the domain alone must not decide it.
        raw = make_raw(
            source_url="https://www.linkedin.com/jobs/view/1",
            application_url=None,
        )

        assert classify_application_type(raw).is_eligible

    def test_a_per_posting_one_click_flow_is_still_caught(self) -> None:
        raw = make_raw(
            application_url="https://www.linkedin.com/jobs/easyapply/1234"
        )

        assert classify_application_type(raw).is_quick_apply

    @pytest.mark.parametrize(
        "title",
        [
            "Financial Analyst (Easy Apply)",
            "Financial Analyst - Quick Apply",
            "Instant Apply: Financial Analyst",
            "Financial Analyst | 1-Click Apply",
        ],
    )
    def test_a_title_that_advertises_quick_apply_is_refused(
        self, title: str
    ) -> None:
        assert classify_application_type(make_raw(title=title)).is_quick_apply

    def test_a_badge_in_the_listing_header_is_refused(self) -> None:
        raw = make_raw(description="Easy Apply\n\n" + LONG_DESCRIPTION)

        assert classify_application_type(raw).is_quick_apply

    def test_the_phrase_deep_in_the_body_is_not_a_badge(self) -> None:
        """A long application that happens to mention the words is still one.

        Rejecting on any occurrence anywhere in the posting removes real jobs,
        and there is nothing in the result set to show they were ever there.
        """
        body = (
            "A" * (DESCRIPTION_SCAN_CHARS + 200)
            + "\nUnlike our competitors we do not offer easy apply."
        )
        raw = make_raw(description=body)

        assert classify_application_type(raw).is_eligible

    @pytest.mark.parametrize(
        "text",
        [
            "Apply now to join our team.",
            "We review applications quickly and respond within a week.",
            "Our easy application process takes about twenty minutes.",
            "Click the link below to apply on our careers site.",
        ],
    )
    def test_ordinary_application_wording_is_not_quick_apply(
        self, text: str
    ) -> None:
        # These appear in a large share of real postings. Matching them would
        # quietly delete most of the market.
        raw = make_raw(description=f"{text}\n\n{LONG_DESCRIPTION}")

        assert classify_application_type(raw).is_eligible

    def test_no_evidence_yields_unknown_not_a_guess(self) -> None:
        verdict = classify_application_type(make_raw())

        assert verdict.application_type is ApplicationType.UNKNOWN
        assert verdict.is_eligible
        assert "no quick-apply evidence" in verdict.reason


# ---------------------------------------------------------------------------
# Per-portal classification, from the captured fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def cards() -> list[dict[str, Any]]:
    """Every captured SimplyHired card, as the portal returned it."""
    return walk_for(load("simplyhired_financial_analyst.json"), "indeedApply")


class TestSimplyHired:
    """The portal the phase singled out: it returns both kinds."""

    def test_quick_apply_cards_are_refused(
        self, cards: list[dict[str, Any]]
    ) -> None:
        portal = SimplyHiredPortal()
        quick = [c for c in cards if c.get("indeedApply") is True]

        assert quick, "the fixture has no Quick Apply cards to check"
        for card in quick:
            raw = portal._to_raw_job(card)
            assert raw is not None
            assert classify_application_type(raw).is_quick_apply, card.get("title")

    def test_long_application_cards_are_kept(
        self, cards: list[dict[str, Any]]
    ) -> None:
        portal = SimplyHiredPortal()
        long_form = [c for c in cards if c.get("indeedApply") is False]

        assert long_form, "the fixture has no long-application cards to check"
        for card in long_form:
            raw = portal._to_raw_job(card)
            assert raw is not None
            verdict = classify_application_type(raw)
            assert verdict.is_eligible, card.get("title")
            assert verdict.application_type is ApplicationType.LONG_APPLICATION

    def test_the_split_matches_what_the_portal_actually_returned(
        self, cards: list[dict[str, Any]]
    ) -> None:
        # Recorded so a silent change in what SimplyHired publishes is visible
        # as a test failure rather than as a quietly smaller result set.
        assert len(cards) == 60
        assert sum(1 for c in cards if c.get("indeedApply") is True) == 44
        assert sum(1 for c in cards if c.get("indeedApply") is False) == 16

    def test_a_card_without_the_flag_is_unknown_not_assumed(self) -> None:
        assert (
            SimplyHiredPortal._application_type({}) is ApplicationType.UNKNOWN
        )


class TestJobright:
    def test_an_easy_apply_record_is_refused(self) -> None:
        assert (
            JobrightPortal._application_type({"jobtargetEasyapply": True})
            is ApplicationType.QUICK_APPLY
        )

    def test_the_captured_records_are_long_applications(self) -> None:
        portal = JobrightPortal()
        records = walk_for(load("jobright_financial_analyst.json"), "jobResult")

        assert records
        for record in records:
            raw = portal._to_raw_job(record)
            assert raw is not None
            verdict = classify_application_type(raw)
            assert verdict.is_eligible
            assert verdict.application_type is ApplicationType.LONG_APPLICATION

    def test_a_record_without_the_flag_is_unknown(self) -> None:
        assert JobrightPortal._application_type({}) is ApplicationType.UNKNOWN


class TestHiringCafe:
    def test_it_declares_unknown_because_it_publishes_no_flag(self) -> None:
        # Documented limitation: asserting a long application here would skip
        # the destination check that actually protects this portal.
        assert HiringCafePortal._application_type({}) is ApplicationType.UNKNOWN

    def test_the_captured_listings_are_eligible(self) -> None:
        portal = HiringCafePortal()
        records = walk_for(load("hiringcafe_financial_analyst.json"), "apply_url")

        assert records
        for record in records:
            raw = portal._to_raw_job(record, slug="financial-analyst")
            assert raw is not None
            assert classify_application_type(raw).is_eligible

    def test_a_quick_apply_destination_is_still_caught(self) -> None:
        """The fallback that stands in for the flag this portal lacks."""
        portal = HiringCafePortal()
        records = walk_for(load("hiringcafe_financial_analyst.json"), "apply_url")
        record = dict(records[0])
        record["apply_url"] = "https://apply.indeed.com/indeedapply/form/resume"

        raw = portal._to_raw_job(record, slug="financial-analyst")
        assert raw is not None
        assert classify_application_type(raw).is_quick_apply


class TestGreenhouse:
    def test_every_captured_posting_is_a_long_application(self) -> None:
        portal = GreenhousePortal()
        records = load("greenhouse_financial_analyst.json")["stripe"]["jobs"]

        assert records
        for record in records:
            raw = portal._to_raw_job(record, board="stripe")
            if raw is None:
                continue
            verdict = classify_application_type(raw)
            assert verdict.application_type is ApplicationType.LONG_APPLICATION


class TestScaleJobs:
    def test_an_ats_destination_is_a_long_application(self) -> None:
        assert (
            ScaleJobsPortal._application_type({"sourceType": "ats"})
            is ApplicationType.LONG_APPLICATION
        )

    def test_a_non_ats_source_is_unknown_rather_than_waved_through(self) -> None:
        # The portal is trusted for what it says, not beyond it.
        for source_type in ("jobboard", "aggregator", "", None):
            assert (
                ScaleJobsPortal._application_type({"sourceType": source_type})
                is ApplicationType.UNKNOWN
            )

    def test_every_captured_listing_is_a_long_application(self) -> None:
        portal = ScaleJobsPortal()
        payload = load("scalejobs_financial_analyst.json")
        records: list[dict[str, Any]] = []
        for entries in payload.values():
            for entry in entries:
                records.extend(entry.get("jobs") or [])

        assert len(records) == 80
        for record in records:
            raw = portal._to_raw_job(record)
            if raw is None:
                continue
            verdict = classify_application_type(raw)
            assert verdict.application_type is ApplicationType.LONG_APPLICATION


# ---------------------------------------------------------------------------
# The gate inside normalisation
# ---------------------------------------------------------------------------


class TestNormalisationGate:
    def test_a_quick_apply_listing_is_rejected_with_its_own_reason(self) -> None:
        raw = make_raw(
            raw_payload=declare_application_type({}, ApplicationType.QUICK_APPLY)
        )
        result = normalize_raw_job(raw)

        assert isinstance(result, RejectedJob)
        assert result.reason == RejectionReason.QUICK_APPLY
        assert result.reason == QUICK_APPLY_REJECTION

    def test_a_long_application_normalises_as_before(self) -> None:
        result = normalize_raw_job(
            make_raw(
                raw_payload=declare_application_type(
                    {}, ApplicationType.LONG_APPLICATION
                )
            )
        )

        assert not isinstance(result, RejectedJob)
        assert result.title.display == "Financial Analyst"

    def test_an_unknown_listing_is_admitted(self) -> None:
        assert not isinstance(normalize_raw_job(make_raw()), RejectedJob)

    def test_the_us_filter_runs_first(self) -> None:
        """Ordering is specified: normalise, US filter, then application type.

        A non-US quick-apply listing must be reported as non-US, because that
        is the first rule it broke and the one the summary should attribute it
        to.
        """
        raw = make_raw(
            location="London, United Kingdom",
            raw_payload=declare_application_type({}, ApplicationType.QUICK_APPLY),
        )
        result = normalize_raw_job(raw)

        assert isinstance(result, RejectedJob)
        assert result.reason == RejectionReason.NON_US_LOCATION

    def test_an_untitled_listing_is_still_rejected_for_its_title(self) -> None:
        # The application filter must not displace the earlier structural gates.
        raw = make_raw(
            title="   ",
            raw_payload=declare_application_type({}, ApplicationType.QUICK_APPLY),
        )
        result = normalize_raw_job(raw)

        assert isinstance(result, RejectedJob)
        assert result.reason == RejectionReason.NO_TITLE

    def test_rejection_happens_before_any_canonical_key_exists(self) -> None:
        """The filter runs before canonicalisation, so a quick-apply listing
        can never claim the canonical identity of a real vacancy."""
        quick = make_raw(
            raw_payload=declare_application_type({}, ApplicationType.QUICK_APPLY)
        )
        real = make_raw(
            external_id="job-2",
            raw_payload=declare_application_type(
                {}, ApplicationType.LONG_APPLICATION
            ),
        )

        rejected = normalize_raw_job(quick)
        accepted = normalize_raw_job(real)

        assert isinstance(rejected, RejectedJob)
        assert not hasattr(rejected, "canonical_key")
        assert accepted.canonical_key
