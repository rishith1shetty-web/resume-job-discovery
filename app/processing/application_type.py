"""Deciding whether a listing wants a real application or one click.

Phase 13. This product exists to surface jobs worth applying to properly, so a
listing that resolves to "Easy Apply", "Quick Apply", "Apply instantly" or a
one-click submission against an aggregator profile is refused before it is ever
matched against a resume.

# Where the verdict comes from, in order

1. **What the portal said.** If the adapter declared a type through
   ``RawJob.raw_payload[APPLICATION_TYPE_KEY]``, that wins outright. The portal
   is the only party that can see its own metadata - SimplyHired's
   ``indeedApply``, Jobright's ``jobtargetEasyapply``, Scale.jobs'
   ``sourceType`` - and a heuristic here must never overrule a fact there.

2. **Where the application actually goes.** A destination on a known one-click
   host is quick apply whatever the text claims, because that is the form the
   user will land on.

3. **What the listing says about itself.** Specific, near-unambiguous phrases
   only: "easy apply", "quick apply", "1-click apply". These are matched
   against the title and a bounded prefix of the description.

4. **Nothing.** ``UNKNOWN``, which is eligible.

# Why the text rules are so narrow

This runs over the employer's own posting text, where a loose pattern does real
damage. "Apply now" appears in most postings and means nothing; "applications
are processed quickly" is not a quick apply; a Workday form that mentions
"one click" in a benefits blurb is still a full application. Every phrase here
had to be one that a long application would not plausibly contain, and the
description is only read as far as the header region where such a badge would
appear - scanning the whole body finds the phrase in prose and rejects good
jobs.

The asymmetry is deliberate. A false *negative* costs one listing out of
hundreds. A false *positive* silently removes a real vacancy the user wanted,
and there is nothing in the result set to show it was ever there. So when the
evidence is thin, this returns ``UNKNOWN`` and the job is kept.

# What this is not

It is not a judgement of quality, seniority or employer. It is not the role
gate, and it never looks at whether the job suits the resume. It answers one
question - how much of an application is being asked for - and nothing else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.core.constants import ApplicationType
from app.core.logging import get_logger
from app.portals.base import APPLICATION_TYPE_KEY, RawJob

logger = get_logger(__name__)

#: Recorded on ``raw_jobs.rejection_reason`` when a listing is refused here.
QUICK_APPLY_REJECTION = "quick_apply_application"

#: How far into a description a self-declared apply badge is believed.
#:
#: Portals put "Easy Apply" in the card header, which lands at the top of a
#: composed description. Deeper than this the match is almost always prose from
#: the employer's own text, and treating that as a badge rejects real jobs.
DESCRIPTION_SCAN_CHARS = 400

#: Hosts that *are* an application form submitted from a stored profile. A
#: listing whose apply destination is one of these is quick apply whatever the
#: wording says.
#:
#: Only hosts that are the apply surface itself. A host that merely *lists*
#: jobs is not here, and that distinction is load-bearing: a listing page on a
#: big job site usually hands off to the employer's ATS, so treating the site
#: as one-click rejects every real vacancy it carries. LinkedIn is the
#: instructive case - ``linkedin.com/jobs/view/...`` is a listing page, and
#: whether it is Easy Apply is a property of the individual posting, not of the
#: domain. It was on this list briefly and rejected a Greenhouse application
#: that happened to be listed there, which is exactly the false positive this
#: module is written to avoid. Per-posting one-click flows are caught by
#: ``QUICK_APPLY_PATH_HINTS`` and by the portal's own declaration instead.
QUICK_APPLY_HOSTS: frozenset[str] = frozenset(
    {
        "apply.indeed.com",
        "smartapply.indeed.com",
        "my.indeed.com",
        "easyapply.co",
    }
)

#: Path fragments that mark a one-click flow on a host that also serves normal
#: pages. Matched on the path only, never on the query string.
QUICK_APPLY_PATH_HINTS: tuple[str, ...] = (
    "/easyapply",
    "/easy-apply",
    "/quickapply",
    "/quick-apply",
    "/instantapply",
    "/instant-apply",
    "/oneclick",
    "/one-click",
)

#: Phrases a long application would not plausibly contain. Word-bounded, so
#: "easy application process" does not trip "easy apply".
_QUICK_APPLY_PHRASES: tuple[str, ...] = (
    r"easy\s*apply",
    r"quick\s*apply",
    r"instant\s*apply",
    r"apply\s+instantly",
    r"one[\s-]*click\s+appl(?:y|ication)",
    r"1[\s-]*click\s+appl(?:y|ication)",
    r"single[\s-]*click\s+appl(?:y|ication)",
    r"apply\s+with\s+(?:your\s+)?(?:indeed|linkedin)\s+profile",
)

_QUICK_APPLY_RE = re.compile(
    r"\b(?:" + "|".join(_QUICK_APPLY_PHRASES) + r")\b", re.IGNORECASE
)


@dataclass(frozen=True, slots=True)
class ApplicationVerdict:
    """What was decided, and the evidence that decided it.

    The evidence is kept because "why was this job dropped" has to be
    answerable. A filter that removes listings without saying why is
    indistinguishable from a bug.
    """

    application_type: ApplicationType
    reason: str
    evidence: str | None = None

    @property
    def is_eligible(self) -> bool:
        return self.application_type.is_eligible

    @property
    def is_quick_apply(self) -> bool:
        return self.application_type is ApplicationType.QUICK_APPLY


def declared_application_type(raw: RawJob) -> ApplicationType | None:
    """The type the adapter declared, if it declared a usable one.

    An unrecognised value is treated as no declaration rather than as an error:
    the rest of the pipeline can still classify the listing on evidence, and a
    portal shipping a typo should cost precision, not the whole batch.
    """
    declared = (raw.raw_payload or {}).get(APPLICATION_TYPE_KEY)
    if declared is None:
        return None
    try:
        return ApplicationType(str(declared))
    except ValueError:
        logger.warning(
            "application_type_declaration_unrecognised",
            portal=raw.source,
            external_id=raw.external_id,
            declared=str(declared)[:64],
        )
        return None


def _host_verdict(url: str | None) -> ApplicationVerdict | None:
    """Whether a destination is a one-click apply surface."""
    if not url:
        return None

    try:
        parts = urlsplit(url)
    except ValueError:  # pragma: no cover - defensive
        return None

    host = (parts.hostname or "").casefold()
    path = (parts.path or "").casefold()

    if host in QUICK_APPLY_HOSTS:
        return ApplicationVerdict(
            ApplicationType.QUICK_APPLY,
            reason="the application URL is a one-click apply host",
            evidence=host,
        )

    for hint in QUICK_APPLY_PATH_HINTS:
        if hint in path:
            return ApplicationVerdict(
                ApplicationType.QUICK_APPLY,
                reason="the application URL is a one-click apply flow",
                evidence=f"{host}{path}"[:120],
            )

    return None


def _text_verdict(raw: RawJob) -> ApplicationVerdict | None:
    """Whether the listing advertises a quick apply in its own words."""
    match = _QUICK_APPLY_RE.search(raw.title or "")
    if match:
        return ApplicationVerdict(
            ApplicationType.QUICK_APPLY,
            reason="the job title advertises a quick apply",
            evidence=match.group(0),
        )

    header = (raw.description or "")[:DESCRIPTION_SCAN_CHARS]
    match = _QUICK_APPLY_RE.search(header)
    if match:
        return ApplicationVerdict(
            ApplicationType.QUICK_APPLY,
            reason="the listing header advertises a quick apply",
            evidence=match.group(0),
        )

    return None


def classify_application_type(raw: RawJob) -> ApplicationVerdict:
    """Decide how much of an application one collected listing demands.

    Portal-agnostic by construction: the only payload key read is the reserved
    ``APPLICATION_TYPE_KEY``, so adding a portal never means editing this file
    (CLAUDE.md section 4).
    """
    declared = declared_application_type(raw)
    if declared is not None and declared is not ApplicationType.UNKNOWN:
        return ApplicationVerdict(
            declared,
            reason=f"{raw.source} reported this as {declared.value}",
            evidence=APPLICATION_TYPE_KEY,
        )

    # Only where the user actually applies. ``source_url`` is where the listing
    # was *found*, which is a different claim: a portal listing a job that
    # applies on the employer's ATS would otherwise be judged by the portal's
    # own domain, and every such job - the normal case - would be refused.
    destination = raw.application_url or raw.source_url
    verdict = _host_verdict(destination)
    if verdict is not None:
        return verdict

    verdict = _text_verdict(raw)
    if verdict is not None:
        return verdict

    # Nothing said quick apply. That is not the same as knowing it is a long
    # one, and the distinction is preserved rather than rounded off.
    return ApplicationVerdict(
        ApplicationType.UNKNOWN,
        reason="no quick-apply evidence was found",
    )


__all__ = [
    "DESCRIPTION_SCAN_CHARS",
    "QUICK_APPLY_HOSTS",
    "QUICK_APPLY_PATH_HINTS",
    "QUICK_APPLY_REJECTION",
    "ApplicationVerdict",
    "classify_application_type",
    "declared_application_type",
]
