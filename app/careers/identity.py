"""Stable identity for a career page, so a decision about one can be stored.

Career pages are discovery output rather than stored records: a run computes
them and the summary counts them, but no table holds them. A user decision has
to outlive the run that produced it, so it needs a key that two runs will agree
on without a row to point at.

# Why not the company name

``"Capital One"``, ``"Capital One, N.A."`` and ``"CAPITAL ONE"`` are one
employer, and a name is not an identity: two employers share a name often
enough that keying on it would merge unrelated decisions. The URL is the thing
the user actually acted on.

# Why not the full URL either

This was the first attempt, and live verification killed it.
``verify_career_url`` trims a posting link back to the company's careers root
only where the URL's shape makes the root certain. For an ATS it cannot read -
Paycom, say - it correctly hands back what the posting published, which looks
like::

    .../jobs/ViewJobDetails?job=349427&clientkey=9A1A...

That URL carries a *specific opening*. Two runs surfacing two different jobs at
one employer produce two different URLs, so keying on the URL gave the same
company a new identity on every run and the decision silently stopped
suppressing anything. The failure is invisible: the page just keeps coming
back.

# What is actually stable

The employer, and where their careers presence lives. So the key is
``normalize_company`` - the same company key the discovery layer already
deduplicates companies with - paired with the **host** of the canonicalised
career URL.

Both halves earn their place. The host alone is useless: every Paycom-hosted
employer shares it. The company alone is what the brief rules out, and rightly:
two unrelated employers can normalise to the same name. Together they are
stable across runs, distinguish employers sharing an ATS host, and collapse two
deep links into one company's career page - which is the behaviour a user
expects when they mark an employer done.

The host comes from ``canonical_url``, the project's existing URL comparison
form, which forces https and drops ``www.``. No second normalisation scheme is
introduced here.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from app.processing.companies import normalize_company
from app.processing.text import stable_hash
from app.processing.urls import canonical_url

#: Width of the stored key. Matches the identifier widths used elsewhere in
#: the schema and is far past the collision range for one user's decisions.
CAREER_PAGE_KEY_LENGTH = 40


def career_page_host(official_career_url: str | None) -> str:
    """The canonical host of a career URL, or ``""`` when there is none."""
    canonical = canonical_url(official_career_url)
    if not canonical:
        return ""
    return (urlsplit(canonical).hostname or "").casefold()


def career_page_key(
    *, official_career_url: str | None, company_name: str | None = None
) -> str | None:
    """The stable identity of one career page, or None when there is none.

    Built from the employer and the host their careers presence lives on, for
    the reasons in the module docstring - most importantly that the full URL is
    not stable across runs for every ATS.

    Returns ``None`` rather than inventing a key when the company cannot be
    normalised at all. A decision that cannot be matched back to the page on
    the next run is worse than no decision, because it fails silently: the page
    the user dismissed simply keeps reappearing.
    """
    company = normalize_company(company_name)
    company_key = company.key if company else ""
    host = career_page_host(official_career_url)

    if not company_key:
        # Without an employer there is nothing stable to key on. A bare host is
        # shared by every employer on that ATS, so it cannot stand alone.
        return None

    return stable_hash("career", company_key, host, length=CAREER_PAGE_KEY_LENGTH)


__all__ = ["CAREER_PAGE_KEY_LENGTH", "career_page_key"]
