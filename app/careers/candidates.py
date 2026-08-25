"""Turning an observed posting URL into candidate career-portal URLs.

Every candidate produced here is **derived from a URL a portal actually
published**. Nothing is manufactured: this module never composes
``<company>.com/careers`` from a company name, never guesses a subdomain, and
never returns a URL whose host was not in the observed link. The only
transformation applied is removing path segments, which cannot invent a
destination that was not already being pointed into.

# Why this replaced trimming to a root

The previous rule trimmed a posting link back to the host root whenever the
host's leading label was not obviously the vendor's. Measured against real
collected postings, that produced ``recruiting.paylocity.com/`` (a Company ID
login page), ``workforcenow.adp.com/`` (an ADP login redirect),
``recruiting2.ultipro.com/`` (404), ``deaconess.rec.pro.ukg.net/`` (404) and
``cvshealth.wd1.myworkdayjobs.com/`` (406). In every one of those cases the
posting itself carried the employer's real board path -
``/sal1016salo/JobBoard/<board-id>/…``, ``/DEA1005DEAC/JobBoard/<board-id>/…``,
``/sites/SouthernCompanyJobs/…`` - and the trim threw away exactly the segment
that identified the employer.

So the rule is inverted. Depth is *preserved* and given up one segment at a
time, and the host root is the last thing tried rather than the first - except
where the host is the employer's own recruiting site, where the root is the
right answer and is tried first.

# Ordering is about cost, not correctness

Nothing here decides anything. Every candidate is fetched and judged by
``app.careers.validation``; this only decides what to try first so the common
case costs one request instead of four. A bad ordering wastes time. It cannot
admit a bad page, because ordering is not a verdict.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

from app.careers.verification import (
    CAREERS_HOST_LABELS,
    PATH_ADDRESSED_ATS,
    resembles_company,
)
from app.core.constants import JobUrlKind
from app.processing.urls import normalize_url

#: Candidates generated from one observed URL. Past this the returns vanish -
#: the remaining prefixes are single generic segments - while the cost is a
#: real HTTP request each.
MAX_CANDIDATES_PER_URL = 5

#: Candidates tried for one company across all of its observed URLs. The brief
#: asks that a company not be discarded when its first URL fails, and this is
#: how far that is taken before the company is genuinely out of evidence.
MAX_CANDIDATES_PER_COMPANY = 9

#: Workday addresses a board as ``<tenant>.wd<n>.myworkdayjobs.com/<site>``,
#: optionally behind a locale segment. Vendor URL grammar, not a company list:
#: no employer name appears here, and the tenant and site are read out of the
#: observed URL rather than supplied.
_WORKDAY_HOST = re.compile(
    r"^(?P<tenant>[^.]+)\.(?P<wd>wd\d+)\.(?P<domain>myworkdayjobs|myworkdaysite)\.com$"
)

#: A leading ``/en-US/`` style segment that Workday and several others place
#: above the board name.
_LOCALE_SEGMENT = re.compile(r"^[a-z]{2}([-_][A-Za-z]{2})?$")

#: iCIMS serves an employer's searchable board at ``/jobs/search`` on the
#: employer's own iCIMS subdomain. Again vendor grammar: the subdomain that
#: identifies the employer comes from the observed URL.
_ICIMS_HOST_SUFFIX = ".icims.com"


def _segments(path: str) -> list[str]:
    return [segment for segment in path.split("/") if segment]


def _build(parts: tuple[str, str], segments: list[str]) -> str:
    scheme, netloc = parts
    path = "/" + "/".join(segments) if segments else "/"
    return urlunsplit((scheme, netloc, path, "", ""))


def _is_employer_careers_host(host: str) -> bool:
    """Is the host itself the employer's recruiting site?

    ``careers.example.com`` and ``jobs.example.com`` exist to serve one
    employer's openings, so their root is a career page rather than a vendor
    lobby. This is only ever consulted for hosts already classified as the
    employer's own (``EMPLOYER_CAREER``), so the leading label is being read
    for what kind of site it is, never for whose it is.
    """
    labels = [label for label in host.split(".") if label]
    return len(labels) >= 2 and labels[0].casefold() in CAREERS_HOST_LABELS


def _vendor_board_roots(url: str) -> list[str]:
    """Board roots implied by a vendor's own URL grammar.

    Shortcuts, not authorities. Each one still gets fetched and judged like any
    other candidate; knowing the shape only means the first request is usually
    the right one instead of the fourth.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").casefold()
    segments = _segments(parts.path)
    base = (parts.scheme, parts.netloc)
    roots: list[str] = []

    if not segments:
        return roots

    # Path-addressed boards: the employer is the first path segment.
    # ``job-boards.greenhouse.io/acme/jobs/123`` -> ``/acme``.
    if host in PATH_ADDRESSED_ATS:
        roots.append(_build(base, segments[:1]))

    workday = _WORKDAY_HOST.match(host)
    if workday is not None:
        # ``/en-US/site/job/...`` and ``/site/job/...`` both name the board in
        # the first non-locale segment.
        index = 1 if _LOCALE_SEGMENT.match(segments[0]) and len(segments) > 1 else 0
        roots.append(_build(base, segments[: index + 1]))

    if host.endswith(_ICIMS_HOST_SUFFIX):
        roots.append(_build(base, ["jobs", "search"]))

    return roots


#: Path segments that mark the *posting* rather than the board. A prefix
#: ending in one of these is still generated - Workday's ``/site/job`` proves
#: guessing wrong is easy - but it is tried after the cleaner prefixes.
_DETAIL_MARKERS = frozenset(
    {
        "job",
        "jobdetail",
        "jobdetails",
        "job-detail",
        "job-details",
        "opportunitydetail",
        "viewjobdetails",
        "jobintroduction",
        "requisitions",
        "detail",
        "details",
        "view",
        "apply",
    }
)

_NUMERIC = re.compile(r"^\d{3,}$")
_UUID_ISH = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-", re.I)
_TRAILING_ID = re.compile(r"[-_]\d{4,}$")


def _looks_like_a_posting(segment: str) -> bool:
    lowered = segment.casefold()
    return (
        lowered in _DETAIL_MARKERS
        or bool(_NUMERIC.match(segment))
        or bool(_UUID_ISH.match(segment))
        or bool(_TRAILING_ID.search(segment))
    )


def career_url_candidates(
    url: str | None, *, kind: JobUrlKind, company: str | None
) -> list[str]:
    """Candidate career-portal URLs for one observed posting link.

    Ordered strongest first:

    1. the employer's own recruiting host at its root, when the host is one;
    2. board roots implied by the vendor's URL grammar;
    3. path prefixes, **deepest first**, so employer and tenant identifiers
       survive as long as possible;
    4. the host root, last, and only where it could plausibly be the
       employer's rather than the vendor's lobby;
    5. the observed URL itself, as the final fallback.

    Returns an empty list for a URL that cannot be parsed. Never returns a host
    that was not in ``url``.
    """
    normalized = normalize_url(url)
    if normalized is None:
        return []

    parts = urlsplit(normalized)
    host = (parts.hostname or "").casefold()
    if not host:
        return []

    base = (parts.scheme, parts.netloc)
    segments = _segments(parts.path)
    root = _build(base, [])

    ordered: list[str] = []

    employer_host = kind is JobUrlKind.EMPLOYER_CAREER
    if employer_host and _is_employer_careers_host(host):
        ordered.append(root)

    ordered.extend(_vendor_board_roots(normalized))

    # Deepest first, dropping one segment at a time, never the observed URL.
    prefixes = [segments[:depth] for depth in range(len(segments) - 1, 0, -1)]
    clean = [p for p in prefixes if not _looks_like_a_posting(p[-1])]
    detailish = [p for p in prefixes if _looks_like_a_posting(p[-1])]
    ordered.extend(_build(base, prefix) for prefix in clean)
    ordered.extend(_build(base, prefix) for prefix in detailish)

    # The bare host root. For the employer's own domain this is their homepage,
    # which the evidence rules will reject unless it genuinely serves jobs. For
    # a vendor host it is the product's lobby, so it is only worth a request
    # when the leading label looks like this employer's own tenant.
    labels = [label for label in host.split(".") if label]
    if employer_host:
        ordered.append(root)
    elif len(labels) >= 3 and company and resembles_company(labels[0], company):
        ordered.append(root)

    ordered.append(normalized)

    seen: set[str] = set()
    unique: list[str] = []
    for candidate in ordered:
        if candidate in seen:
            continue
        seen.add(candidate)
        unique.append(candidate)

    return unique[:MAX_CANDIDATES_PER_URL]


__all__ = [
    "MAX_CANDIDATES_PER_COMPANY",
    "MAX_CANDIDATES_PER_URL",
    "career_url_candidates",
]
