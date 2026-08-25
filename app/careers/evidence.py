"""Reading a fetched page to decide what it actually is.

Everything here operates on a body that was really downloaded. No rule looks
at the URL's shape to decide whether the page is good - the URL only ever says
*whose* the page should be, so that the page can be checked against it.

# Order matters, and one ordering is mandatory

Authentication detection runs **first**, before any counting of the words
"Careers" or "Jobs". This is not a stylistic preference. The page that started
this work says::

    Paylocity Payroll + HR
    Redirecting to Login Page

and other gated pages are worse: a Company ID form is usually served under a
heading that says "Careers", with a "Jobs" link in the navigation. Any rule
that counted encouraging words first would pass that page before ever noticing
the login form. So the gate is checked first and, if found, nothing else is
consulted.

# What counts as proof of a career portal

Not vocabulary. "Join our team", "Careers", "Life at <company>" appear on
marketing pages, login walls and 404s alike. What counts is *structure that
only a job portal has*:

* links that address individual postings, several of them;
* a search or filter affordance aimed at jobs, together with at least one
  posting link;
* or, for a board that renders in the browser, the openings its own public
  listing endpoint returns (see ``app.careers.listings``).

A page that has none of these is not rejected as fraudulent - it is reported as
*unverified*, because absence of evidence is what it is. Either way the company
is omitted, which is what the brief asks for: better to leave a company out
than to send someone to a page they cannot use.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.processing.text import comparison_key

#: Phrases that mean "you must identify yourself before you can look".
#:
#: Split from the login vocabulary below because these are unambiguous: a
#: public job board never asks for an organisation identifier. Paylocity's
#: Company ID page is the archetype, but nothing here names a vendor - the
#: phrasing is what is recognised, so any tenant-selection wall is caught.
_TENANT_GATE = re.compile(
    r"""
    (company|client|organi[sz]ation|employer|tenant|customer|account)
    \s*(id|identifier|code|number|key)\b
    | \bcompanyid\b
    | enter\s+your\s+(company|organi[sz]ation|client)\b
    | \bsubdomain\b\s*(is\s*)?required
    """,
    re.I | re.X,
)

#: Phrases that mean "sign in to continue". Weaker evidence than the above: a
#: perfectly good careers page carries a "Sign in" link for returning
#: applicants, and rejecting on that alone would throw away real employers -
#: measured, it discarded a working ``careers.<employer>.com``. So a hit here
#: only counts when the page has *no* job evidence at all, which is what
#: ``AccessVerdict.gated`` encodes.
_LOGIN_WALL = re.compile(
    r"""
    redirecting\s+to\s+(the\s+)?login
    | (please\s+)?(log\s?in|sign\s?in)\s+to\s+(continue|access|view|proceed)
    | you\s+must\s+(log\s?in|sign\s?in|be\s+logged\s+in)
    | (employee|associate|staff|internal)\s+(log\s?in|sign\s?in|portal\s+log)
    | single\s+sign[-\s]?on\s+required
    | authentication\s+required
    | session\s+(has\s+)?(expired|timed\s+out)
    | \bunauthori[sz]ed\b
    """,
    re.I | re.X,
)

#: A password field is the strongest structural signal of a login form: no
#: public job search has one.
_PASSWORD_INPUT = re.compile(r"<input[^>]*type\s*=\s*[\"']?password", re.I)

#: Pages that resolved but hold nothing. Matched against the start of the text
#: so that a footer link reading "Page not found" cannot condemn a good page.
_DEAD_PAGE = re.compile(
    r"""
    job\s+not\s+found
    | (this\s+)?(job|position|posting|requisition)\s+(is\s+)?
      (no\s+longer\s+(available|active|open)|has\s+(been\s+)?(closed|expired|filled))
    | (page|job\s+board)\s+(not\s+found|does\s+not\s+exist)
    | \b404\b\s*[-:–]?\s*(not\s+found|page)
    | we\s+(couldn'?t|could\s+not|can'?t)\s+find\s+(what|the\s+page)
    | the\s+page\s+you\s+(are\s+looking\s+for|requested)\s+(does\s+not|doesn'?t|
      cannot|can'?t)
    | sorry,?\s+(that|this)\s+(job|page|posting)\s+(does\s+not|doesn'?t)
    """,
    re.I | re.X,
)

#: Anchors that address one posting. The variety is the point: employers and
#: vendors name this path a dozen ways, and requiring one spelling would
#: silently favour whichever vendors happen to use it.
_POSTING_LINK = re.compile(
    r"""href\s*=\s*["']([^"'\s]*(?:
        /job/ | /jobs/ | /job- | /careers/[^"'\s]*\d
        | jobdetail | job-detail | job_detail | job-details
        | opportunitydetail | viewjob | /vacancy | /vacancies/
        | /position/ | /positions/ | /opening | /posting
        | /requisition | gh_jid= | /apply/ | /p/ | /j/
    )[^"'\s]*)["']""",
    re.I | re.X,
)

#: Affordances that only a job *search* has. Counted, but never sufficient on
#: their own - a marketing page can say "Search jobs" and link to nothing.
_SEARCH_AFFORDANCE = re.compile(
    r"""
    search\s+(for\s+)?(jobs|openings|opportunities|roles|positions|careers)
    | job\s+search
    | (view|see|browse|explore)\s+(all\s+)?(our\s+)?
      (jobs|openings|opportunities|current\s+openings|positions)
    | current\s+(job\s+)?openings
    | open\s+(positions|roles|jobs)
    | \d+\s+(open\s+)?(jobs|positions|openings|results|opportunities)
    | filter\s+by\s+(location|category|department|team)
    | (job|search)\s+results
    """,
    re.I | re.X,
)

#: Markup a search box is built from, aimed at jobs rather than at the site.
_SEARCH_INPUT = re.compile(
    r"""<(?:input|select)[^>]*
        (?:name|id|placeholder|aria-label)\s*=\s*["'][^"']*
        (?:keyword|job|search|location|department|category|req)
    """,
    re.I | re.X,
)

_TAG = re.compile(r"<[^>]+>")
_SCRIPT_OR_STYLE = re.compile(
    r"<(script|style|noscript)\b.*?</\1>", re.I | re.S
)
_WHITESPACE = re.compile(r"\s+")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)

#: How much of the visible text counts as "the top of the page" for gate
#: detection. A login wall announces itself immediately; a footer mentioning
#: an employee portal three screens down is not a wall.
_GATE_WINDOW = 6000

#: Distinct posting links that make a page a listing on their own.
MIN_POSTING_LINKS = 3

#: Below this much visible text the page is a client-rendered shell: there is
#: nothing to read, so nothing can be concluded from reading it. Such a page is
#: never rejected on evidence - it goes to the listing probe instead.
SHELL_TEXT_THRESHOLD = 400


def visible_text(body: str) -> str:
    """The page's readable text, with scripts and markup removed."""
    without_code = _SCRIPT_OR_STYLE.sub(" ", body)
    return _WHITESPACE.sub(" ", _TAG.sub(" ", without_code)).strip()


def page_title(body: str) -> str:
    match = _TITLE.search(body)
    if not match:
        return ""
    return _WHITESPACE.sub(" ", _TAG.sub(" ", match.group(1))).strip()


@dataclass(frozen=True, slots=True)
class PageEvidence:
    """What one fetched page turned out to contain."""

    text: str
    title: str
    #: Distinct hrefs that address an individual posting.
    posting_links: int
    has_search_affordance: bool
    has_search_input: bool
    #: A tenant/organisation identifier is demanded before anything is shown.
    tenant_gate: bool
    #: A login wall is announced, or a password field is present.
    login_gate: bool
    dead_page: bool

    @property
    def is_shell(self) -> bool:
        """Rendered in the browser: there is no text here to judge."""
        return len(self.text) < SHELL_TEXT_THRESHOLD

    @property
    def has_job_evidence(self) -> bool:
        """Does this page demonstrably let someone find a job?

        Two ways to satisfy it, and both require *posting links* - the one
        thing a marketing page and a login page do not have. A search box
        strengthens a single link into evidence; it never substitutes for one.
        """
        if self.posting_links >= MIN_POSTING_LINKS:
            return True
        return self.posting_links >= 1 and (
            self.has_search_affordance or self.has_search_input
        )


def read_page(body: str) -> PageEvidence:
    """Extract every signal from one page body, in one pass.

    Deliberately returns *facts*, not a verdict. The ordering rule that makes
    authentication outrank vocabulary lives in ``access_verdict`` and
    ``app.careers.validation``, where it can be read in one place.
    """
    text = visible_text(body)
    head = text[:_GATE_WINDOW]

    links = {match.group(1) for match in _POSTING_LINK.finditer(body)}

    return PageEvidence(
        text=text,
        title=page_title(body),
        posting_links=len(links),
        has_search_affordance=bool(_SEARCH_AFFORDANCE.search(head)),
        has_search_input=bool(_SEARCH_INPUT.search(body)),
        tenant_gate=bool(_TENANT_GATE.search(head)),
        login_gate=bool(_LOGIN_WALL.search(head)) or bool(_PASSWORD_INPUT.search(body)),
        dead_page=bool(_DEAD_PAGE.search(text[:1500])),
    )


@dataclass(frozen=True, slots=True)
class AccessVerdict:
    """Whether the page can be used without identifying yourself."""

    gated: bool
    reason: str


def access_verdict(evidence: PageEvidence) -> AccessVerdict:
    """Is this page behind an authentication or tenant-selection wall?

    **Called before anything looks for careers vocabulary**, which is the whole
    point of separating it: the Company ID page that prompted this feature says
    "Careers" at the top, and a rule that reads encouraging words first would
    have already passed it.

    A tenant gate is decisive on its own. A login wall counts only when the
    page also fails to show any jobs, because real careers sites carry a
    "Sign in" link for returning applicants - rejecting on that alone was
    measured discarding a working employer careers site.
    """
    if evidence.tenant_gate:
        return AccessVerdict(
            gated=True,
            reason=(
                "The page asks for a company or organisation ID before showing "
                "any jobs, so it is not publicly browsable."
            ),
        )

    if evidence.login_gate and not evidence.has_job_evidence:
        return AccessVerdict(
            gated=True,
            reason=(
                "The page requires signing in before any jobs can be viewed or "
                "searched."
            ),
        )

    return AccessVerdict(gated=False, reason="")


def belongs_to_employer(
    *,
    final_url: str,
    evidence: PageEvidence,
    company: str,
    host_was_employer_domain: bool,
) -> bool:
    """Does this page belong to the employer we think it does?

    Everything is judged against the **final** URL and the body that was
    actually served. That the *requested* URL was on the employer's domain
    proves nothing about where it ended up - a redirect from an employer's own
    careers path into a different company's site was accepted by an earlier
    draft of this function for exactly that reason, and the test that caught it
    is ``test_a_redirect_into_another_company_is_rejected``. So
    ``host_was_employer_domain`` is only consulted for names too short to
    compare at all.

    Any one of these is enough, because employers legitimately publish under
    all of them:

    1. the final host's own domain label is the employer's name, which is the
       ``careers.<employer>.com`` case;
    2. a host label or the first path segment resembles the employer - an ATS
       tenant says whose board it is, usually in an abbreviated form
       ("ridgelineenergy", "northwindanalytics") that a substring test would
       miss;
    3. the employer's name appears in the page title;
    4. the employer's name appears in the body text.
    """
    from app.careers.verification import resembles_company

    key = comparison_key(company).replace(" ", "")
    if len(key) < 4:
        # Too short to distinguish; fall back to the classifier's judgement.
        return host_was_employer_domain

    parts = urlsplit(final_url)
    labels = [label for label in (parts.hostname or "").split(".") if label]
    segments = [segment for segment in parts.path.split("/") if segment]

    # The registrable label plus the leading label: "careers.northwind.com" is
    # checked as "northwind", and "ridgelineenergy.wd1.myworkdayjobs.com" as
    # "ridgelineenergy".
    host_candidates = labels[:1] + (labels[-2:-1] if len(labels) >= 2 else [])
    for candidate in host_candidates + segments[:1]:
        if resembles_company(candidate, company):
            return True

    title_key = comparison_key(evidence.title).replace(" ", "")
    if title_key and key in title_key:
        return True

    body_key = comparison_key(evidence.text[:20000]).replace(" ", "")
    return bool(body_key) and key in body_key


def is_vendor_front_door(
    *, final_url: str, company: str, is_ats_host: bool
) -> bool:
    """Is this the applicant-tracking product's own lobby rather than a board?

    A vendor root with no employer-specific path and no employer-specific
    subdomain is the product's marketing site: ``boards.greenhouse.io``,
    ``jobs.lever.co``, ``workday.com``. It is refused however encouraging its
    contents are, because it is not any particular employer's page.

    Only asked of ATS hosts. An employer's own domain at its root is their
    homepage, which is judged on job evidence like anything else.
    """
    if not is_ats_host:
        return False

    parts = urlsplit(final_url)
    if [segment for segment in parts.path.split("/") if segment]:
        return False

    labels = [label for label in (parts.hostname or "").split(".") if label]
    if len(labels) < 3:
        return True

    from app.careers.verification import GENERIC_HOST_LABELS, resembles_company

    leading = labels[0].casefold()
    if leading in GENERIC_HOST_LABELS:
        return True

    # A tenant subdomain that is the employer's is not a front door, even
    # though it has no path: ``acme.bamboohr.com`` is Acme's board.
    return not resembles_company(leading, company)


__all__ = [
    "MIN_POSTING_LINKS",
    "SHELL_TEXT_THRESHOLD",
    "AccessVerdict",
    "PageEvidence",
    "access_verdict",
    "belongs_to_employer",
    "is_vendor_front_door",
    "page_title",
    "read_page",
    "visible_text",
]
