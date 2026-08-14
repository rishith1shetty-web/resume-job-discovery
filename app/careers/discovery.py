"""Turning collected postings into companies and their career pages.

Pure functions over values: no session, no network, no clock beyond the
``now`` handed in. That is what makes the whole feature testable without a
fixture portal or a live request.

# Where the companies come from

From postings that were actually collected in this run. Nothing is looked up in
a maintained company list, and none of the companies in the brief appears
anywhere in this codebase - a company reaches a result only by having a current
opening that passed the same role gate and the same US rule the job pipeline
uses. That is also what makes the evidence honest: "3 current US openings
matching FP&A Analyst" is a statement about postings that were seen, not an
inference about a well-known employer.

# Why this is not a job score

A career page is not a vacancy, so it does not get a vacancy's score. There is
no description to read, no requirements to compare, no seniority to weigh -
none of the evidence the 0-100 ``final_score`` is built from exists here.
``relevance`` is a separate, company-level judgement built only from what *can*
be observed: how close the observed titles are to the target role, how many
such openings there are, and how direct the career page is. It is named
differently, computed differently, and never mixed into the job ranking.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from app.careers.base import CareerPage, CareerPageDiscovery, CareerPageRequest
from app.careers.verification import VerifiedCareerUrl, verify_career_url  # noqa: TC001
from app.core.constants import JobUrlKind
from app.matching.profile import ResumeMatchProfile
from app.matching.requirements import extract_requirements
from app.matching.roles import RoleVerdict, evaluate_role
from app.processing.companies import NormalizedCompany, companies_match, normalize_company
from app.processing.text import clean_display_text
from app.processing.urls import normalize_and_classify
from app.processing.us_locations import classify_us_location

#: Hard upper bound on career pages per run, independent of the run's
#: ``requested_limit``. Find 20 and Find 100 both get at most this many, and
#: neither number affects the other: career pages never enter the job
#: allocation, so they cannot take a slot from it.
#:
#: It is a ceiling, never a target. A run returns
#: ``min(MAX_CAREER_PAGES, verified relevant companies)`` - if seven companies
#: qualify, seven are returned. Nothing is padded, relaxed or carried over from
#: an earlier run to reach twenty.
MAX_CAREER_PAGES = 20

#: How close an observed title must be to the target role before the company is
#: worth showing. Deliberately *not* the job threshold and with no effect on it:
#: this scores a company, that scores a vacancy. The number is a floor on
#: company-level evidence - a single loosely-matching opening does not clear it,
#: two do.
MIN_CAREER_RELEVANCE = 70.0

#: Role closeness, as a starting relevance. Only verdicts that passed the gate
#: appear: INCOMPATIBLE never reaches here.
_VERDICT_RELEVANCE: dict[RoleVerdict, float] = {
    RoleVerdict.EXACT: 100.0,
    RoleVerdict.VARIATION: 90.0,
    RoleVerdict.STRONG_VARIANT: 80.0,
    RoleVerdict.WEAK_VARIANT: 68.0,
}

#: Rank for choosing the best evidence within one company. Lower is stronger.
_VERDICT_RANK: dict[RoleVerdict, int] = {
    RoleVerdict.EXACT: 0,
    RoleVerdict.VARIATION: 1,
    RoleVerdict.STRONG_VARIANT: 2,
    RoleVerdict.WEAK_VARIANT: 3,
    RoleVerdict.INCOMPATIBLE: 4,
}

#: Extra relevance per additional matching opening, and its ceiling. More
#: openings is better evidence that the employer is genuinely hiring for this
#: role, but it saturates: eight vacancies is not four times the signal of two.
_EVIDENCE_PER_EXTRA_OPENING = 2.0
_MAX_EVIDENCE_BONUS = 8.0

#: The employer's own careers site is a more direct destination than their
#: space on an applicant-tracking system, so it breaks ties in its favour.
_EMPLOYER_DOMAIN_BONUS = 4.0

#: Locations and titles listed on one card before it stops being a summary.
_MAX_LOCATIONS = 3
_MAX_TITLES = 2


@dataclass(slots=True)
class _Accumulator:
    """One company being built up from the postings that mention it."""

    company: NormalizedCompany
    best_verdict: RoleVerdict
    url: VerifiedCareerUrl
    titles: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    opening_count: int = 0

    def absorb(
        self,
        *,
        verdict: RoleVerdict,
        url: VerifiedCareerUrl,
        title: str,
        location: str | None,
        source: str,
    ) -> None:
        self.opening_count += 1

        if _VERDICT_RANK[verdict] < _VERDICT_RANK[self.best_verdict]:
            self.best_verdict = verdict
            # Keep the strongest evidence's title at the front.
            self.titles.insert(0, title)
        elif title not in self.titles:
            self.titles.append(title)

        # An employer's own domain outranks their ATS space, whichever posting
        # happened to arrive first.
        if (
            url.kind is JobUrlKind.EMPLOYER_CAREER
            and self.url.kind is not JobUrlKind.EMPLOYER_CAREER
        ):
            self.url = url

        if location and location not in self.locations:
            self.locations.append(location)
        if source and source not in self.sources:
            self.sources.append(source)


@dataclass(frozen=True, slots=True)
class _Pending:
    """A qualifying opening whose URL did not verify under its own name."""

    company: NormalizedCompany
    verdict: RoleVerdict
    title: str
    location: str | None
    source: str
    #: Registrable hosts this posting's URLs point at.
    hosts: frozenset[str]


def _candidate_hosts(listing: object, company: str) -> frozenset[str]:
    """The hosts a posting's URLs live on, however they classified.

    Read even for URLs that were refused: a refused URL still identifies where
    it points, which is what the reconciliation pass compares.
    """
    hosts: set[str] = set()
    for candidate in (
        getattr(listing, "application_url", None),
        getattr(listing, "source_url", None),
    ):
        classified = normalize_and_classify(candidate, company=company)
        if classified is not None:
            hosts.add(classified.host)
    return frozenset(hosts)


def _relevance(accumulator: _Accumulator) -> float:
    """Company-level relevance from the evidence actually observed."""
    base = _VERDICT_RELEVANCE.get(accumulator.best_verdict, 0.0)
    evidence = min(
        _MAX_EVIDENCE_BONUS,
        _EVIDENCE_PER_EXTRA_OPENING * max(0, accumulator.opening_count - 1),
    )
    direct = (
        _EMPLOYER_DOMAIN_BONUS
        if accumulator.url.kind is JobUrlKind.EMPLOYER_CAREER
        else 0.0
    )
    return round(min(100.0, base + evidence + direct), 2)


def _describe_location(locations: Sequence[str]) -> str:
    """What the card says about where the openings are.

    Falls back to "United States" rather than inventing a city: every opening
    behind a card already passed the US rule, so that much is known even when
    the portal published nothing more specific.
    """
    named = [location for location in locations if location]
    if not named:
        return "United States"
    shown = named[:_MAX_LOCATIONS]
    text = " · ".join(shown)
    if len(named) > _MAX_LOCATIONS:
        text += f" +{len(named) - _MAX_LOCATIONS} more"
    return text


def _describe_role(titles: Sequence[str], fallback: str) -> str:
    """The observed titles, not the resume's role restated.

    Showing what the employer actually calls the job is the useful thing: a
    card reading "Financial Analyst / FP&A Analyst" tells the user what they
    will find on the page.
    """
    distinct = [title for title in titles if title][:_MAX_TITLES]
    return " / ".join(distinct) if distinct else fallback


def _reason(accumulator: _Accumulator, role_display: str) -> str:
    count = accumulator.opening_count
    plural = "s" if count != 1 else ""
    where = (
        "the company's own careers site"
        if accumulator.url.kind is JobUrlKind.EMPLOYER_CAREER
        else "the company's own applicant-tracking board"
    )
    return (
        f"{count} current US opening{plural} matching {role_display}. "
        f"Official career page verified as {where}."
    )


def _find_existing(
    accumulators: list[_Accumulator],
    company: NormalizedCompany,
    url: VerifiedCareerUrl | None,
) -> _Accumulator | None:
    """The accumulator this company belongs to, if one already exists.

    Three tests, strongest first.

    1. **The same normalised name.** "Veeva Systems" and "Veeva Systems, Inc."
       already share a key once the legal suffix is stripped.

    2. **The same verified career page.** This is the test that catches
       "Veeva" against "Veeva Systems", which the name rule below deliberately
       refuses: ``companies_match`` will not fold a single-token name into a
       longer one, because that is what stops "Square" becoming "Square Enix".
       That caution is right for deduplicating *vacancies* and is not weakened
       here. A career page is a stronger signal than a name - two records that
       resolve to the same verified official careers URL are the same
       employer's careers page by definition, whatever each posting called the
       company.

    3. **The project's own employer-similarity rule**, unchanged, for names
       that differ in spelling rather than in shape.
    """
    for accumulator in accumulators:
        if accumulator.company.key == company.key:
            return accumulator

    if url is not None:
        for accumulator in accumulators:
            if accumulator.url.url == url.url:
                return accumulator

    for accumulator in accumulators:
        matched, _ = companies_match(accumulator.company, company)
        if matched:
            return accumulator

    return None


def discover_career_pages(
    listings: Iterable[object],
    *,
    profile: ResumeMatchProfile,
    request: CareerPageRequest,
    now: datetime,
    limit: int = MAX_CAREER_PAGES,
) -> CareerPageDiscovery:
    """Group collected postings into verified company career pages.

    ``listings`` are ``RawJob``-shaped: read by attribute so the function does
    not depend on the portal package, which keeps the two lanes independent.

    Every gate here is a refusal, never a relaxation. A posting must be the
    resume's role, be in the United States, name an employer, and carry a URL
    that verifies as that employer's own recruiting page. A company that fails
    any of them is not shown, and the count is short rather than padded.
    """
    discovery = CareerPageDiscovery()
    accumulators: list[_Accumulator] = []
    #: Companies whose every URL was refused. Kept as a set of keys so an
    #: employer with four unusable links counts once, not four times.
    unverified: set[str] = set()
    #: Openings that were the right role and in the US, but whose URL did not
    #: verify against the name *this* posting used. Reconsidered at the end -
    #: see the reconciliation pass below.
    pending: list[_Pending] = []

    for listing in listings:
        title = clean_display_text(getattr(listing, "title", None))
        if not title:
            continue

        company = normalize_company(getattr(listing, "company", None))
        if company is None:
            # No employer means nothing to verify a career page against.
            continue

        location_text = clean_display_text(getattr(listing, "location", None))

        # The project's own role gate, so a Financial Analyst resume cannot
        # widen into Accountant or Business Analyst here any more than it can
        # in the job pipeline. There is no description to read, which is
        # exactly why this produces a company-level judgement and not a score.
        assessment = evaluate_role(
            profile,
            extract_requirements(title=title, description=None, location=location_text),
        )
        if assessment.verdict is RoleVerdict.INCOMPATIBLE:
            discovery.off_role_openings += 1
            continue

        if not classify_us_location(location_text).is_us:
            discovery.non_us_openings += 1
            continue

        # The employer's application link first: it is the one most likely to
        # be on their own domain or their own ATS space. The listing URL is
        # tried second, and usually belongs to the portal, which is refused.
        verified: VerifiedCareerUrl | None = None
        for candidate in (
            getattr(listing, "application_url", None),
            getattr(listing, "source_url", None),
        ):
            verified = verify_career_url(candidate, company=company.display)
            if verified is not None:
                break

        existing = _find_existing(accumulators, company, verified)

        source = str(getattr(listing, "source", "") or "")

        if verified is None:
            if existing is not None:
                # Already a known company; this opening is still evidence.
                existing.absorb(
                    verdict=assessment.verdict,
                    url=existing.url,
                    title=title,
                    location=location_text,
                    source=source,
                )
                continue

            unverified.add(company.key)
            pending.append(
                _Pending(
                    company=company,
                    verdict=assessment.verdict,
                    title=title,
                    location=location_text,
                    source=source,
                    hosts=_candidate_hosts(listing, company.display),
                )
            )
            continue

        if existing is None:
            accumulators.append(
                _Accumulator(
                    company=company,
                    best_verdict=assessment.verdict,
                    url=verified,
                    titles=[title],
                    locations=[location_text] if location_text else [],
                    sources=[source] if source else [],
                    opening_count=1,
                )
            )
            continue

        if existing.company.key != company.key:
            discovery.merged_companies += 1
        existing.absorb(
            verdict=assessment.verdict,
            url=verified,
            title=title,
            location=location_text,
            source=source,
        )

    # --- Reconciliation -----------------------------------------------------
    # An opening whose URL did not verify against the name *that posting* used,
    # but which points at a career page another posting already verified.
    #
    # This is the mirror of the Veeva case. "careers.veeva.com" verifies for
    # both "Veeva Systems" and "Veeva", because the domain carries the short
    # form. Turn it around - "careers.northwind-analytics.com" for a posting
    # that wrote the employer as "Northwind" - and the host check fails, since
    # accepting a domain that merely *starts with* the company name is what
    # would let "careers.applebees.com" claim Apple.
    #
    # Host equality settles it without loosening anything: the page was already
    # verified for the fuller name, and the same host is the same page.
    for item in pending:
        for accumulator in accumulators:
            if accumulator.url.host not in item.hosts:
                continue
            discovery.merged_companies += 1
            unverified.discard(item.company.key)
            accumulator.absorb(
                verdict=item.verdict,
                url=accumulator.url,
                title=item.title,
                location=item.location,
                source=item.source,
            )
            break

    accepted_keys = {accumulator.company.key for accumulator in accumulators}
    discovery.companies_seen = len(accumulators)
    discovery.unverified_companies = len(unverified - accepted_keys)

    pages: list[CareerPage] = []
    for accumulator in accumulators:
        relevance = _relevance(accumulator)
        if relevance < MIN_CAREER_RELEVANCE:
            # Weak evidence is dropped rather than shown to fill the list.
            continue
        pages.append(
            CareerPage(
                company_name=accumulator.company.display,
                company_key=accumulator.company.key,
                relevant_role=_describe_role(accumulator.titles, request.role_display),
                location=_describe_location(accumulator.locations),
                official_career_url=accumulator.url.url,
                url_kind=accumulator.url.kind,
                opening_count=accumulator.opening_count,
                relevance=relevance,
                reason=_reason(accumulator, request.role_display),
                sources=tuple(accumulator.sources),
                matched_titles=tuple(accumulator.titles[:_MAX_TITLES]),
            )
        )

    # Strongest evidence first, then more openings, then the company key so the
    # order is total and two runs over the same listings agree.
    pages.sort(key=lambda page: (-page.relevance, -page.opening_count, page.company_key))

    ceiling = max(0, min(limit, MAX_CAREER_PAGES))
    discovery.pages = pages[:ceiling]
    return discovery


__all__ = [
    "MAX_CAREER_PAGES",
    "MIN_CAREER_RELEVANCE",
    "discover_career_pages",
]
