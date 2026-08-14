"""Deciding whether a URL is genuinely a company's official career page.

This is the rule the feature stands on. A career page that turns out to be an
Indeed search or a LinkedIn company profile is worse than no result: it sends
the user to the aggregator they were trying to bypass, under a heading that
promised the employer's own site.

Nothing here invents a URL. Every candidate arrives from a posting that was
actually collected, and this module either accepts it, trims it to that
company's own careers root, or refuses it. It also never fetches anything - it
reads strings, exactly like ``app.processing.urls``.

# The rule

A URL is an official career page when ``classify_url`` - which the project
already uses to rank application links - returns either:

``EMPLOYER_CAREER``
    The host demonstrably belongs to the employer. That classifier's own
    docstring puts it well: "that is evidence, not a guess."

``ATS``
    The employer's own space on an applicant-tracking system, which the brief
    explicitly allows ("company-specific Greenhouse careers, company-specific
    Lever careers"). A *bare* ATS root is refused - ``boards.greenhouse.io`` is
    Greenhouse's front door, not a company's career page.

Everything else is refused, which by construction covers LinkedIn, Indeed,
Glassdoor, ZipRecruiter, Jobright, Hiring Cafe, Scale.jobs and every other host
in ``_AGGREGATOR_HOSTS``, plus anything unrecognised. Unrecognised is refused
rather than accepted: an unverifiable URL cannot be shown to be the employer's.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from urllib.parse import urlsplit, urlunsplit

from app.core.constants import JobUrlKind
from app.processing.companies import normalize_company
from app.processing.text import comparison_key
from app.processing.urls import normalize_and_classify

#: The two verdicts that mean "the employer's own recruiting page".
ACCEPTED_KINDS: frozenset[JobUrlKind] = frozenset(
    {JobUrlKind.EMPLOYER_CAREER, JobUrlKind.ATS}
)

#: ATS front doors that address each employer as the first path segment, e.g.
#: ``boards.greenhouse.io/acme``. A fact about those products, not a company
#: list: it is what lets a deep posting link be trimmed to the employer's own
#: board root instead of pointing at one expired vacancy.
_PATH_ADDRESSED_ATS: frozenset[str] = frozenset(
    {
        "boards.greenhouse.io",
        "job-boards.greenhouse.io",
        "jobs.lever.co",
        "jobs.ashbyhq.com",
        "jobs.smartrecruiters.com",
        "apply.workable.com",
        "jobs.jobvite.com",
        "boards.eu.greenhouse.io",
    }
)

#: Host labels that are the product's own, never the employer's. A subdomain
#: from this set carries no company identity.
_GENERIC_LABELS: frozenset[str] = frozenset(
    {"www", "boards", "job-boards", "jobs", "apply", "careers", "api", "my", "app"}
)

#: Path segments that are a careers *section* rather than one vacancy, so a
#: link can be trimmed back to them safely.
_CAREERS_SEGMENTS: frozenset[str] = frozenset(
    {"careers", "career", "jobs", "job", "join-us", "joinus", "work-with-us"}
)

#: Leading host labels that say "this subdomain is the recruiting site".
_CAREERS_HOST_LABELS: frozenset[str] = frozenset(
    {"careers", "career", "jobs", "job", "hiring", "recruiting", "talent"}
)

#: Shortest company label this module will accept as evidence on its own.
#: ``classify_url`` uses six for the general case; five is safe here *because*
#: more is being required alongside it - see ``_is_employer_careers_host``.
_MIN_CORROBORATING_LABEL = 5


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
    if len(labels) < 3 or labels[0] not in _CAREERS_HOST_LABELS:
        return False

    # Slugified, so a hyphenated domain compares against a spaced company name:
    # "northwind-analytics" and "Northwind Analytics" are the same word.
    domain_label = _slugify(labels[-2])
    name = comparison_key(company).replace(" ", "")
    if len(domain_label) < _MIN_CORROBORATING_LABEL or len(name) < 4:
        return False

    return name.startswith(domain_label)


@dataclass(frozen=True, slots=True)
class VerifiedCareerUrl:
    """A URL that passed verification, plus how it was reached."""

    url: str
    kind: JobUrlKind
    host: str
    #: True when the URL was trimmed back to the company's careers root rather
    #: than left pointing at the individual posting it came from.
    trimmed: bool
    #: True when the host or ATS path segment also resembles the company name.
    #: Strengthens the reason text; never the sole basis for acceptance.
    name_corroborated: bool

    @property
    def is_employer_domain(self) -> bool:
        return self.kind is JobUrlKind.EMPLOYER_CAREER


def _labels(host: str) -> list[str]:
    return [label for label in host.split(".") if label]


def _slugify(segment: str) -> str:
    """A host label or path segment reduced to comparable letters.

    Separators go, so "northwind-analytics" and "fitch_group" compare equal to
    "northwindanalytics" and "fitchgroup". Company names lose their spaces the
    same way, which is what puts the two sides in one alphabet.
    """
    return comparison_key(segment.replace("-", " ").replace("_", " ")).replace(" ", "")


def _resembles(segment: str, company: str) -> bool:
    """Whether a host label or path slug looks like this company's name.

    Short segments are ignored: a three-letter slug matches too many names by
    accident.
    """
    slug = _slugify(segment)
    name = comparison_key(company).replace(" ", "")
    if len(slug) < 4 or len(name) < 4:
        return False
    return slug == name or name.startswith(slug) or slug.startswith(name)


def _careers_root(url: str, kind: JobUrlKind, company: str) -> tuple[str, bool]:
    """Trim a posting link back to the company's careers page when that is safe.

    Returns ``(url, trimmed)``. A career page is a *place to look for openings*,
    so pointing at one specific vacancy - which will expire - is the wrong
    destination. But a wrong guess is worse than a deep link, so this only
    trims where the shape of the URL makes the root certain, and otherwise
    hands back what the posting actually published.
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    segments = [segment for segment in parts.path.split("/") if segment]

    if kind is JobUrlKind.ATS:
        # ``boards.greenhouse.io/acme/jobs/123`` -> ``boards.greenhouse.io/acme``.
        if host in _PATH_ADDRESSED_ATS and segments:
            return urlunsplit((parts.scheme, host, f"/{segments[0]}", "", "")), True
        # ``acme.bamboohr.com/careers/42`` -> the employer's own subdomain root,
        # but only when the leading label is the employer's rather than the
        # product's own front door.
        labels = _labels(host)
        if len(labels) >= 3 and labels[0] not in _GENERIC_LABELS:
            return urlunsplit((parts.scheme, host, "/", "", "")), True
        return url, False

    # The employer's own domain. A dedicated careers host has nothing above its
    # root to get wrong.
    labels = _labels(host)
    if labels and labels[0] in {"careers", "career", "jobs", "job"}:
        return urlunsplit((parts.scheme, host, "/", "", "")), True

    # ``acme.com/careers/roles/123`` -> ``acme.com/careers``.
    if segments and segments[0].casefold() in _CAREERS_SEGMENTS:
        return (
            urlunsplit((parts.scheme, host, f"/{segments[0]}", "", "")),
            len(segments) > 1,
        )

    return url, False


def verify_career_url(
    url: str | None, *, company: str | None
) -> VerifiedCareerUrl | None:
    """Accept a URL as a company's official career page, or refuse it.

    Returns None for anything that cannot be shown to belong to the employer -
    an aggregator, a portal listing, an unusable string, or a host nobody can
    tie to this company. Refusing is the safe direction: the result set is
    allowed to be short, but every URL in it has to be what it claims.
    """
    if not company or not company.strip():
        # Without an employer there is nothing to verify the host against, and
        # a career page with no company is not a result anyone can use.
        return None

    classified = normalize_and_classify(url, company=company)
    if classified is None:
        return None

    normalized = normalize_company(company)
    name = normalized.display if normalized else company

    kind = classified.kind
    if kind not in ACCEPTED_KINDS:
        # One narrow rescue, and only upwards from UNKNOWN: a recruiting
        # subdomain on the employer's own name. An aggregator or portal
        # listing is never reconsidered - those are refused on the host, which
        # no amount of corroboration can change.
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
        # A bare ATS root is the product's front page, not an employer's page.
        has_company_space = bool(segments) or (
            len(labels) >= 3 and labels[0] not in _GENERIC_LABELS
        )
        if not has_company_space:
            return None
        corroborated = any(_resembles(segment, name) for segment in segments[:1]) or (
            len(labels) >= 3 and _resembles(labels[0], name)
        )
    else:
        # EMPLOYER_CAREER is only returned when the host already matched the
        # employer's name, so corroboration is inherent rather than extra.
        corroborated = True

    final_url, trimmed = _careers_root(classified.url, classified.kind, name)

    return VerifiedCareerUrl(
        url=final_url,
        kind=classified.kind,
        host=classified.host,
        trimmed=trimmed,
        name_corroborated=corroborated,
    )


__all__ = ["ACCEPTED_KINDS", "VerifiedCareerUrl", "verify_career_url"]
