"""URL validation, normalisation and classification.

Three separate jobs, kept separate on purpose:

``normalize_url``   produce a clean, still-working URL a user can open
``canonical_url``   produce a comparison key, which nobody should ever open
``classify_url``    say what kind of destination it is, without guessing

Nothing here fetches a URL, follows a redirect, submits a form or clicks
anything. Phase 5 reads strings. The user opens the link and applies manually
(CLAUDE.md section 1).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.core.constants import JOB_URL_PREFERENCE, JobUrlKind
from app.processing.text import comparison_key

#: Only these schemes are ever accepted. A javascript: or data: "application
#: link" is not a link, and mailto: cannot be opened as a job posting.
ALLOWED_SCHEMES = frozenset({"http", "https"})

#: Longest URL the database column holds.
MAX_URL_LENGTH = 2048

# Analytics parameters, safe to drop: they identify the referrer, not the job.
_TRACKING_PARAMS = frozenset(
    {
        "gclid",
        "fbclid",
        "msclkid",
        "dclid",
        "yclid",
        "twclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "_ga",
        "_gl",
        "trk",
        "trkinfo",
        "originalsubdomain",
        "refid",
        "position",
        "pagenum",
        "gh_src",
        "lever-origin",
        "lever-source",
        "src_trk",
        "utm_content",
        "utm_campaign",
        "utm_medium",
        "utm_source",
        "utm_term",
        "utm_id",
        "s",
        "cid",
        "campaignid",
        "adid",
    }
)
_TRACKING_PREFIXES = ("utm_", "pk_", "mtm_", "hsa_", "at_")

# Parameters that carry the job's identity. Dropping one of these breaks the
# link: Greenhouse boards resolve the vacancy from gh_jid, and several ATS
# products put the requisition id in the query rather than the path.
_IDENTITY_PARAMS = frozenset(
    {
        "gh_jid",
        "jid",
        "jobid",
        "job_id",
        "job",
        "id",
        "req",
        "reqid",
        "requisitionid",
        "postingid",
        "posting_id",
        "vacancyid",
        "currentjobid",
        "positionid",
        "rid",
        "pid",
        "jk",
        "jobkey",
    }
)

# Applicant-tracking systems. A URL on one of these hosts is where an
# application is actually submitted, which is as direct as it gets short of the
# employer's own careers site.
_ATS_HOSTS = (
    "greenhouse.io",
    "lever.co",
    "myworkdayjobs.com",
    "myworkdaysite.com",
    "workday.com",
    "taleo.net",
    "icims.com",
    "smartrecruiters.com",
    "ashbyhq.com",
    "jobvite.com",
    "bamboohr.com",
    "successfactors.com",
    "successfactors.eu",
    "brassring.com",
    "workable.com",
    "recruitee.com",
    "breezy.hr",
    "teamtailor.com",
    "paylocity.com",
    "dayforcehcm.com",
    "ultipro.com",
    "adp.com",
    "eightfold.ai",
    "phenompeople.com",
    "avature.net",
    "oraclecloud.com",
    "jobs.silkroad.com",
    "applytojob.com",
    "clearcompany.com",
    "hire.trakstar.com",
    "rippling.com",
    "gem.com",
    "polymer.co",
)

# Job boards and aggregators. A link to one of these is a listing, not an
# application: the user still has to click through on the other side.
_AGGREGATOR_HOSTS = (
    "jobright.ai",
    "linkedin.com",
    "indeed.com",
    "glassdoor.com",
    "glassdoor.co.uk",
    "ziprecruiter.com",
    "simplyhired.com",
    "monster.com",
    "dice.com",
    "careerbuilder.com",
    "totaljobs.com",
    "reed.co.uk",
    "seek.com.au",
    "naukri.com",
    "hiring.cafe",
    "hiringcafe.com",
    "wellfound.com",
    "angel.co",
    "builtin.com",
    "themuse.com",
    "jobs2careers.com",
    "talent.com",
    "adzuna.com",
    "jooble.org",
    "lensa.com",
    "upwork.com",
    "qureos.com",
    "scale.jobs",
    "roberthalf.com",
    "insightglobal.com",
)

_HOST_PREFIX = re.compile(r"^www\d*\.")


class UrlError(ValueError):
    """A URL that cannot be used as a job or application link."""


@dataclass(frozen=True, slots=True)
class NormalizedUrl:
    """One URL, in the three forms the rest of Phase 5 needs."""

    #: Cleaned and still openable. This is what a user clicks.
    url: str
    #: Comparison key. Never displayed, never opened.
    canonical: str
    kind: JobUrlKind
    host: str

    @property
    def preference_rank(self) -> int:
        """Lower is more direct. See ``JOB_URL_PREFERENCE``."""
        return JOB_URL_PREFERENCE[self.kind]


def _registrable_host(host: str) -> str:
    return _HOST_PREFIX.sub("", host.casefold())


def _host_matches(host: str, suffixes: tuple[str, ...]) -> str | None:
    """Return the matched suffix when ``host`` is it, or a subdomain of it."""
    bare = _registrable_host(host)
    for suffix in suffixes:
        if bare == suffix or bare.endswith(f".{suffix}"):
            return suffix
    return None


def is_valid_url(value: str | None) -> bool:
    """True when this is an http(s) URL with a host, and short enough to store."""
    if not value or len(value) > MAX_URL_LENGTH:
        return False

    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return False

    return bool(parts.scheme.lower() in ALLOWED_SCHEMES and parts.netloc and parts.hostname)


def _clean_query(query: str) -> str:
    """Drop tracking parameters, keep everything that identifies the job.

    Identity parameters are checked first: ``gh_src`` is Greenhouse's referrer
    tag and goes, ``gh_jid`` is the vacancy and stays.
    """
    kept: list[tuple[str, str]] = []

    for name, value in parse_qsl(query, keep_blank_values=True):
        lowered = name.casefold()

        if lowered in _IDENTITY_PARAMS:
            kept.append((name, value))
        elif lowered in _TRACKING_PARAMS or lowered.startswith(_TRACKING_PREFIXES):
            continue
        else:
            kept.append((name, value))

    return urlencode(kept)


def normalize_url(value: str | None) -> str | None:
    """Clean a URL for use, or return None when it is not usable.

    Removes the fragment - no job link needs one, and it defeats comparison -
    lower-cases the scheme and host, drops a default port, and strips known
    tracking parameters. Unrecognised parameters are kept: an unknown parameter
    is more likely to be part of the job's identity than analytics, and a link
    that no longer opens is worse than a slightly untidy one.
    """
    if not is_valid_url(value):
        return None

    assert value is not None  # narrowed by is_valid_url
    parts = urlsplit(value.strip())

    scheme = parts.scheme.casefold()
    host = (parts.hostname or "").casefold()
    port = parts.port
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        host = f"{host}:{port}"

    path = parts.path or "/"
    # Trailing slashes are cosmetic everywhere except the root.
    if len(path) > 1:
        path = path.rstrip("/") or "/"

    cleaned = urlunsplit((scheme, host, path, _clean_query(parts.query), ""))

    return cleaned if len(cleaned) <= MAX_URL_LENGTH else None


def canonical_url(value: str | None) -> str | None:
    """Comparison key for a URL. Not for display and not for opening.

    Goes further than ``normalize_url``: drops ``www.``, forces https, and
    sorts the surviving parameters so two portals linking to the same posting
    with the parameters in a different order still compare equal.
    """
    normalized = normalize_url(value)
    if normalized is None:
        return None

    parts = urlsplit(normalized)
    host = _registrable_host(parts.netloc)
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))

    return urlunsplit(("https", host, parts.path.casefold(), query, ""))


def classify_url(
    value: str | None,
    *,
    company: str | None = None,
    portal_slug: str | None = None,
) -> JobUrlKind:
    """Say what kind of destination a URL is.

    ``EMPLOYER_CAREER`` is only ever returned when the host's own name matches
    the employer's - that is evidence, not a guess. Anything unrecognised stays
    ``UNKNOWN`` rather than being promoted to employer-direct, because the
    ordering in ``JOB_URL_PREFERENCE`` would then rank an unverified link above
    a working portal one (CLAUDE.md section 9, Phase 5 brief).
    """
    if not is_valid_url(value):
        return JobUrlKind.UNKNOWN

    assert value is not None
    host = urlsplit(value.strip()).hostname or ""
    bare = _registrable_host(host)

    if _host_matches(bare, _ATS_HOSTS):
        return JobUrlKind.ATS

    aggregator = _host_matches(bare, _AGGREGATOR_HOSTS)
    if aggregator is not None:
        # The portal's own listing page, versus a hop through some other board.
        if portal_slug and comparison_key(portal_slug).replace(" ", "") in bare.replace(
            ".", ""
        ):
            return JobUrlKind.PORTAL_LISTING
        return JobUrlKind.AGGREGATOR_REDIRECT

    if company and _host_is_employer(bare, company):
        return JobUrlKind.EMPLOYER_CAREER

    return JobUrlKind.UNKNOWN


def _host_is_employer(bare_host: str, company: str) -> bool:
    """Does this host demonstrably belong to this employer?

    Compares the registrable label against the employer's name with spaces and
    punctuation removed: "careers.berkadia.com" for "Berkadia" is a match,
    "boards.example.com" for "Berkadia" is not. Short labels are ignored
    because a two or three letter domain matches too many names by accident.
    """
    labels = bare_host.split(".")
    if len(labels) < 2:
        return False

    company_key = comparison_key(company).replace(" ", "")
    if len(company_key) < 4:
        return False

    # The registrable label, e.g. "berkadia" in "careers.berkadia.com".
    domain_label = labels[-2]
    if len(domain_label) < 4:
        return False

    if domain_label == company_key:
        return True

    # "berkadia" for "Berkadia Commercial Mortgage" is the same employer. The
    # length floor stops a generic label like "bank" from claiming every
    # company whose name happens to start with it.
    return len(domain_label) >= 6 and company_key.startswith(domain_label)


def normalize_and_classify(
    value: str | None,
    *,
    company: str | None = None,
    portal_slug: str | None = None,
) -> NormalizedUrl | None:
    """Normalise and classify in one step. None when the URL is unusable."""
    normalized = normalize_url(value)
    if normalized is None:
        return None

    canonical = canonical_url(normalized)
    if canonical is None:
        return None

    return NormalizedUrl(
        url=normalized,
        canonical=canonical,
        kind=classify_url(normalized, company=company, portal_slug=portal_slug),
        host=_registrable_host(urlsplit(normalized).netloc),
    )
