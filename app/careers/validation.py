"""Proving - or refusing - that a candidate URL is a usable career portal.

This is where "verified" is earned. Every candidate is fetched, its redirects
are followed and re-checked, and the destination is judged on what it actually
served. A URL's shape is never a reason to accept it here; the shape only
decided that fetching was worth the request.

# The order of the checks is the specification

Each step can only refuse, and the order is chosen so that the cheapest and
most decisive refusals come first, and so that the one ordering the brief
mandates is structural rather than incidental:

1. **Could we look at all?** Blocked address, robots refusal, timeout,
   transport failure. None of these is a statement about the page.
2. **What did the server say?** 404/410/400 is dead; 401 is a login wall by
   definition; 403/406/429/5xx is the site declining *us*, which we report as
   unavailable rather than pretending it is a verdict about the employer.
3. **Is it a page we can read?** A PDF is not a career portal.
4. **Is it gated?** ``access_verdict`` - tenant/Company-ID first, then login
   walls. **Before any careers vocabulary is counted**, because the page that
   prompted this work says "Careers" at the top and asks for a Company ID
   underneath.
5. **Is it dead in the body?** "Job Not Found" served with a 200, which several
   ATS products do.
6. **Is it the vendor's lobby?** A product front door belongs to no employer.
7. **Is it this employer's?** Checked against the *final* URL, so a redirect
   into another company's site is caught.
8. **Does it actually offer jobs?** Posting links, or a search affordance with
   at least one posting link, or - for a board that renders in the browser -
   the openings its own public listing endpoint returns.

Only a candidate that survives all eight is ``VERIFIED``. Everything else is
recorded with its cause and omitted from the user's results.

# Refusing is not failing

A company with no verifiable page is dropped. That is the intended behaviour,
stated in the brief: it is better to return eleven usable career portals than
twenty of which nine are login pages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.careers.candidates import (
    MAX_CANDIDATES_PER_COMPANY,
    career_url_candidates,
)
from app.careers.evidence import (
    PageEvidence,
    access_verdict,
    belongs_to_employer,
    is_vendor_front_door,
    read_page,
)
from app.careers.fetching import CareerPageFetcher, FetchOutcome, FetchResult
from app.careers.listings import ListingProbe, probe_listings
from app.careers.verification import CandidateCareerUrl
from app.core.constants import CareerPageStatus, JobUrlKind
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Statuses that mean "the page itself is wrong, and will still be wrong
#: tomorrow". Cached for longer than the ones that mean "we could not look".
TERMINAL_STATUSES: frozenset[CareerPageStatus] = frozenset(
    {CareerPageStatus.REJECTED, CareerPageStatus.UNVERIFIED}
)

#: HTTP statuses that condemn the page rather than our request.
_DEAD_STATUSES = frozenset({400, 404, 410})

#: A login wall by definition. Separated from 403, which is the site declining
#: an automated client and says nothing about whether a person could browse.
_AUTH_STATUS = 401


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """The verdict on one candidate URL."""

    status: CareerPageStatus
    #: The destination a user should be sent to: the end of the redirect
    #: chain, not the URL we asked for. Only set when verified.
    url: str | None
    kind: JobUrlKind | None
    reason: str
    checked_at: datetime
    #: Openings the page or its listing endpoint showed. Evidence for the card,
    #: never job results.
    opening_titles: tuple[str, ...] = ()
    listed_total: int | None = None

    @property
    def verified(self) -> bool:
        return self.status is CareerPageStatus.VERIFIED

    @property
    def is_terminal(self) -> bool:
        """True when re-asking soon would not change the answer."""
        return self.status in TERMINAL_STATUSES


def _result(
    status: CareerPageStatus,
    reason: str,
    *,
    now: datetime,
    url: str | None = None,
    kind: JobUrlKind | None = None,
    probe: ListingProbe | None = None,
) -> ValidationResult:
    return ValidationResult(
        status=status,
        url=url,
        kind=kind,
        reason=reason,
        checked_at=now,
        opening_titles=probe.titles if probe else (),
        listed_total=probe.total if probe else None,
    )


def _transport_reason(fetched: FetchResult) -> tuple[CareerPageStatus, str]:
    """Why nothing was read, in terms a user could act on."""
    if fetched.outcome == FetchOutcome.BLOCKED:
        return (
            CareerPageStatus.REJECTED,
            f"The link could not be opened safely: {fetched.detail}",
        )
    if fetched.outcome == FetchOutcome.DISALLOWED:
        return (CareerPageStatus.UNAVAILABLE, fetched.detail)
    if fetched.outcome == FetchOutcome.REDIRECT_LIMIT:
        return (
            CareerPageStatus.REJECTED,
            f"The link did not settle on a destination: {fetched.detail}",
        )
    return (
        CareerPageStatus.UNAVAILABLE,
        f"The careers site could not be reached: {fetched.detail}",
    )


def _describe_success(
    evidence: PageEvidence, probe: ListingProbe | None, *, employer_domain: bool
) -> str:
    where = (
        "the employer's own careers site"
        if employer_domain
        else "the employer's own applicant-tracking board"
    )
    if probe is not None and probe.has_openings:
        count = probe.total or len(probe.titles)
        return (
            f"Opened and checked: {where}, publicly browsable, "
            f"listing {count} current opening{'s' if count != 1 else ''}."
        )
    if evidence.posting_links:
        return (
            f"Opened and checked: {where}, publicly browsable, with "
            f"{evidence.posting_links} job posting"
            f"{'s' if evidence.posting_links != 1 else ''} listed."
        )
    return f"Opened and checked: {where}, publicly browsable job search."


async def validate_candidate(
    candidate_url: str,
    *,
    company: str,
    kind: JobUrlKind,
    fetcher: CareerPageFetcher,
    now: datetime | None = None,
) -> ValidationResult:
    """Fetch one candidate URL and decide what it is.

    The steps run in the order documented at the top of this module. Each one
    can only refuse; a candidate is verified by surviving all of them.
    """
    moment = now or datetime.now(UTC)

    # 1. Could we look at all?
    fetched = await fetcher.fetch(candidate_url)
    if not fetched.ok:
        status, reason = _transport_reason(fetched)
        return _result(status, reason, now=moment)

    final_url = fetched.final_url
    status_code = fetched.status_code or 0

    # 2. What did the server say?
    if status_code == _AUTH_STATUS:
        return _result(
            CareerPageStatus.REJECTED,
            "The page requires signing in before any jobs can be seen "
            f"(HTTP {status_code}).",
            now=moment,
        )
    if status_code in _DEAD_STATUSES:
        return _result(
            CareerPageStatus.REJECTED,
            f"The page is not there (HTTP {status_code}).",
            now=moment,
        )
    if status_code >= 400:
        return _result(
            CareerPageStatus.UNAVAILABLE,
            f"The site declined this automated request (HTTP {status_code}), "
            "so the page could not be checked.",
            now=moment,
        )

    # 3. Is it a page we can read?
    if not fetched.is_html or not fetched.body:
        # A board that renders in the browser gets its one chance here: the
        # shell is unreadable, but its public listing endpoint is not.
        probe = await probe_listings(fetcher, final_url)
        if probe is not None and probe.has_openings:
            return _result(
                CareerPageStatus.VERIFIED,
                _describe_success(
                    read_page(""), probe, employer_domain=kind is JobUrlKind.EMPLOYER_CAREER
                ),
                now=moment,
                url=final_url,
                kind=kind,
                probe=probe,
            )
        return _result(
            CareerPageStatus.UNVERIFIED,
            "The page returned no readable content, so its job listings could "
            "not be confirmed.",
            now=moment,
        )

    evidence = read_page(fetched.body)

    # 4. Is it gated? Before any careers vocabulary is counted.
    access = access_verdict(evidence)
    if access.gated:
        return _result(CareerPageStatus.REJECTED, access.reason, now=moment)

    # 5. Is it dead in the body?
    if evidence.dead_page:
        return _result(
            CareerPageStatus.REJECTED,
            "The page loaded but reports that the job or board no longer "
            "exists.",
            now=moment,
        )

    # 6. Is it the vendor's lobby rather than an employer's board?
    if is_vendor_front_door(
        final_url=final_url, company=company, is_ats_host=kind is JobUrlKind.ATS
    ):
        return _result(
            CareerPageStatus.REJECTED,
            "That is the recruiting product's own front page, not this "
            "employer's job board.",
            now=moment,
        )

    # 7. Is it this employer's? Checked on the final URL, after redirects.
    if not belongs_to_employer(
        final_url=final_url,
        evidence=evidence,
        company=company,
        host_was_employer_domain=kind is JobUrlKind.EMPLOYER_CAREER,
    ):
        return _result(
            CareerPageStatus.REJECTED,
            "The page that opened does not appear to belong to this employer.",
            now=moment,
        )

    # 8. Does it actually offer jobs?
    if evidence.has_job_evidence:
        return _result(
            CareerPageStatus.VERIFIED,
            _describe_success(
                evidence, None, employer_domain=kind is JobUrlKind.EMPLOYER_CAREER
            ),
            now=moment,
            url=final_url,
            kind=kind,
        )

    probe = await probe_listings(fetcher, final_url)
    if probe is not None and probe.has_openings:
        return _result(
            CareerPageStatus.VERIFIED,
            _describe_success(
                evidence, probe, employer_domain=kind is JobUrlKind.EMPLOYER_CAREER
            ),
            now=moment,
            url=final_url,
            kind=kind,
            probe=probe,
        )

    if evidence.is_shell:
        return _result(
            CareerPageStatus.UNVERIFIED,
            "The page builds its job list in the browser, so the openings "
            "could not be confirmed from here.",
            now=moment,
        )

    return _result(
        CareerPageStatus.UNVERIFIED,
        "The page opened but shows no job listings or job search, so it is "
        "not usable as a careers portal.",
        now=moment,
    )


@dataclass(slots=True)
class CompanyValidation:
    """The outcome of trying every candidate one company had."""

    result: ValidationResult
    #: Every candidate URL actually fetched, in the order tried. Recorded so a
    #: run can explain what it looked at rather than only what it concluded.
    attempted: list[str] = field(default_factory=list)


async def validate_company(
    candidates: list[CandidateCareerUrl],
    *,
    company: str,
    fetcher: CareerPageFetcher,
    now: datetime | None = None,
    max_candidates: int = MAX_CANDIDATES_PER_COMPANY,
) -> CompanyValidation:
    """Find this employer's usable career portal among its observed URLs.

    A company is not discarded because its first link failed. Every URL its
    postings supplied is expanded into candidates and tried in turn, strongest
    first, until one verifies or the evidence runs out - which is what the
    brief asks for and what stops one expired posting from removing an
    employer that has a perfectly good board.

    The reason reported when nothing verifies is the *most informative* one
    seen, not the last: "asks for a Company ID" tells the operator more than
    "could not be reached", and which candidate happened to be tried last is an
    accident of ordering.
    """
    moment = now or datetime.now(UTC)
    attempted: list[str] = []
    best: ValidationResult | None = None

    #: How much a refusal explains. Higher wins when reporting why a company
    #: was dropped.
    informativeness = {
        CareerPageStatus.REJECTED: 3,
        CareerPageStatus.UNVERIFIED: 2,
        CareerPageStatus.UNAVAILABLE: 1,
    }

    for candidate in candidates:
        for url in career_url_candidates(
            candidate.url, kind=candidate.kind, company=company
        ):
            if len(attempted) >= max_candidates:
                break
            if url in attempted:
                continue
            attempted.append(url)

            result = await validate_candidate(
                url,
                company=company,
                kind=candidate.kind,
                fetcher=fetcher,
                now=moment,
            )
            if result.verified:
                return CompanyValidation(result=result, attempted=attempted)

            if best is None or informativeness.get(
                result.status, 0
            ) > informativeness.get(best.status, 0):
                best = result

        if len(attempted) >= max_candidates:
            break

    if best is None:
        best = _result(
            CareerPageStatus.UNVERIFIED,
            "No career page URL could be derived from this company's postings.",
            now=moment,
        )

    return CompanyValidation(result=best, attempted=attempted)


__all__ = [
    "TERMINAL_STATUSES",
    "CompanyValidation",
    "ValidationResult",
    "validate_candidate",
    "validate_company",
]
