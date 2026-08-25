"""Deciding whether a URL is worth *fetching* as a company's career page.

This module is a filter, not a verdict. It reads strings - exactly like
``app.processing.urls`` - and answers one narrow question: is this URL on a
host that could belong to this employer, so that fetching it might teach us
something? A URL that passes here is a **candidate**. It becomes verified only
in ``app.careers.validation``, and only by being fetched and inspected.

# What changed, and why

An earlier version of this module was the whole of verification: it accepted a
URL on the strength of its shape and then trimmed it back to a guessed careers
root. Measured against real collected postings, that shipped users to
``recruiting.paylocity.com/`` (a Company ID login page),
``workforcenow.adp.com/`` (an ADP login redirect), ``recruiting2.ultipro.com/``
(404), ``deaconess.rec.pro.ukg.net/`` (404) and
``cvshealth.wd1.myworkdayjobs.com/`` (406) - six dead or gated destinations out
of twelve, every one of them announced to the user as "official career page
verified".

Both halves of that failure are gone. The trimming moved to
``app.careers.candidates``, which preserves the employer's path instead of
discarding it and proposes *several* candidates rather than asserting one. The
claim of verification moved to ``app.careers.validation``, which fetches.

What remains here is the cheap refusal that is still worth making before any
request: an aggregator, a portal listing page, or a host nobody can tie to this
employer is never worth a round trip.

# The rule

A URL is a candidate when ``classify_url`` - which the project already uses to
rank application links - returns either:

``EMPLOYER_CAREER``
    The host demonstrably belongs to the employer. That classifier's own
    docstring puts it well: "that is evidence, not a guess."

``ATS``
    The employer's space on an applicant-tracking system, which the brief
    explicitly allows. A *bare* vendor root with nothing employer-specific
    about it is refused here and, if it somehow arrives another way, refused
    again on the evidence after fetching.

Everything else is refused, which by construction covers LinkedIn, Indeed,
Glassdoor, ZipRecruiter, Jobright, Hiring Cafe, Scale.jobs and every other host
in ``_AGGREGATOR_HOSTS``, plus anything unrecognised.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from urllib.parse import urlsplit

from app.core.constants import JobUrlKind
from app.processing.companies import normalize_company
from app.processing.text import comparison_key
from app.processing.urls import normalize_and_classify

#: The two verdicts that mean "this host could be the employer's recruiting
#: presence", and so is worth one request to find out.
ACCEPTED_KINDS: frozenset[JobUrlKind] = frozenset(
    {JobUrlKind.EMPLOYER_CAREER, JobUrlKind.ATS}
)

#: ATS front doors that address each employer as the first path segment, e.g.
#: ``boards.greenhouse.io/acme``. A fact about those products, not a company
#: list: it is what lets ``app.careers.candidates`` propose the employer's own
#: board root as the first thing to try.
PATH_ADDRESSED_ATS: frozenset[str] = frozenset(
    {
        "boards.greenhouse.io",
        "job-boards.greenhouse.io",
        "boards.eu.greenhouse.io",
        "jobs.lever.co",
        "jobs.eu.lever.co",
        "jobs.ashbyhq.com",
        "jobs.smartrecruiters.com",
        "careers.smartrecruiters.com",
        "apply.workable.com",
        "jobs.workable.com",
        "jobs.jobvite.com",
        "boards.rippling.com",
        "jobs.gem.com",
    }
)

#: Host labels that are the product's own, never the employer's. A subdomain
#: from this set carries no company identity.
GENERIC_HOST_LABELS: frozenset[str] = frozenset(
    {"www", "boards", "job-boards", "jobs", "apply", "careers", "api", "my", "app"}
)

#: Leading host labels that say "this subdomain is the recruiting site".
CAREERS_HOST_LABELS: frozenset[str] = frozenset(
    {"careers", "career", "jobs", "job", "hiring", "recruiting", "talent"}
)

#: Shortest company label this module will accept as evidence on its own.
#: ``classify_url`` uses six for the general case; five is safe here *because*
#: more is being required alongside it - see ``_is_employer_careers_host``.
_MIN_CORROBORATING_LABEL = 5


def _labels(host: str) -> list[str]:
    return [label for label in host.split(".") if label]


def _slugify(segment: str) -> str:
    """A host label or path segment reduced to comparable letters.

    Separators go, so "northwind-analytics" and "fitch_group" compare equal to
    "northwindanalytics" and "fitchgroup". Company names lose their spaces the
    same way, which is what puts the two sides in one alphabet.
    """
    return comparison_key(segment.replace("-", " ").replace("_", " ")).replace(" ", "")


def resembles_company(segment: str, company: str) -> bool:
    """Whether a host label or path slug looks like this company's name.

    Short segments are ignored: a three-letter slug matches too many names by
    accident. Used as corroboration and as an ordering hint - never as the sole
    reason to accept a destination, which only fetching can establish.
    """
    slug = _slugify(segment)
    name = comparison_key(company).replace(" ", "")
    if len(slug) < 4 or len(name) < 4:
        return False
    return slug == name or name.startswith(slug) or slug.startswith(name)


def _is_employer_careers_host(host: str, company: str) -> bool:
    """Is this a ``careers.<company>.com``-shaped host for this employer?

    ``classify_url`` refuses ``careers.veeva.com`` for "Veeva Systems": it
    allows a domain label to be a prefix of the company name only from six
    characters, and "veeva" is five. That floor is right where it lives -
    it decides which *application link* outranks which, across every job - and
    is not changed here.

    This is a narrower question with more evidence attached. Acceptance needs
    all three of: a leading label that announces a recruiting site, a company
    name that *starts with* the domain label, and a label long enough not to
    collide by accident. The direction matters and is deliberately one-way:
    "veevasystems".startswith("veeva") accepts Veeva, while "apple" does not
    start with "applebees", so ``careers.applebees.com`` cannot claim Apple.
    """
    labels = _labels(host)
    if len(labels) < 3 or labels[0] not in CAREERS_HOST_LABELS:
        return False

    domain_label = _slugify(labels[-2])
    name = comparison_key(company).replace(" ", "")
    if len(domain_label) < _MIN_CORROBORATING_LABEL or len(name) < 4:
        return False

    return name.startswith(domain_label)


@dataclass(frozen=True, slots=True)
class CandidateCareerUrl:
    """An observed URL that is worth fetching as this employer's career page.

    Deliberately *not* called verified. It has not been opened. The name is the
    guard rail: nothing downstream can mistake this for a checked destination,
    because the type does not claim to be one.
    """

    url: str
    kind: JobUrlKind
    host: str
    #: True when the host or ATS path segment also resembles the company name.
    #: Strengthens the later reason text and orders candidates; never on its
    #: own a reason to show a page to a user.
    name_corroborated: bool

    @property
    def is_employer_domain(self) -> bool:
        return self.kind is JobUrlKind.EMPLOYER_CAREER


def admit_career_url(
    url: str | None, *, company: str | None
) -> CandidateCareerUrl | None:
    """Accept a URL as worth fetching for this employer, or refuse it.

    Returns None for anything that cannot be tied to the employer even in
    principle - an aggregator, a portal listing, an unusable string, or a bare
    vendor lobby. Refusing is the safe direction and it is also the cheap one:
    a refusal here is a request not made.
    """
    if not company or not company.strip():
        # Without an employer there is nothing to check the host against, and a
        # career page with no company is not a result anyone can use.
        return None

    classified = normalize_and_classify(url, company=company)
    if classified is None:
        return None

    normalized = normalize_company(company)
    name = normalized.display if normalized else company

    kind = classified.kind
    if kind not in ACCEPTED_KINDS:
        # One narrow rescue, and only upwards from UNKNOWN: a recruiting
        # subdomain on the employer's own name. An aggregator or portal listing
        # is never reconsidered - those are refused on the host, which no
        # amount of corroboration can change.
        if kind is not JobUrlKind.UNKNOWN or not _is_employer_careers_host(
            classified.host, name
        ):
            return None
        kind = JobUrlKind.EMPLOYER_CAREER

    classified = replace(classified, kind=kind)

    if classified.kind is JobUrlKind.ATS:
        parts = urlsplit(classified.url)
        host = parts.hostname or ""
        segments = [segment for segment in parts.path.split("/") if segment]
        labels = _labels(host)
        # A bare vendor root is the product's front page, not an employer's.
        has_company_space = bool(segments) or (
            len(labels) >= 3 and labels[0] not in GENERIC_HOST_LABELS
        )
        if not has_company_space:
            return None
        corroborated = any(
            resembles_company(segment, name) for segment in segments[:1]
        ) or (len(labels) >= 3 and resembles_company(labels[0], name))
    else:
        # EMPLOYER_CAREER is only returned when the host already matched the
        # employer's name, so corroboration is inherent rather than extra.
        corroborated = True

    return CandidateCareerUrl(
        url=classified.url,
        kind=classified.kind,
        host=classified.host,
        name_corroborated=corroborated,
    )


__all__ = [
    "ACCEPTED_KINDS",
    "CAREERS_HOST_LABELS",
    "GENERIC_HOST_LABELS",
    "PATH_ADDRESSED_ATS",
    "CandidateCareerUrl",
    "admit_career_url",
    "resembles_company",
]
