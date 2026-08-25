"""A career-page fetcher that answers from a script instead of the network.

Career-page verification is now defined by what a page *serves*, so the tests
that matter are the ones that hand it a real body and check the verdict. This
stands in for the network so those tests are deterministic and offline: it is
the same class the production lane uses, with one method replaced.

Deliberately a subclass of the real ``CareerPageFetcher`` rather than a
look-alike. Everything except the transport - the per-run response cache, the
listing-probe hook, the context-manager lifecycle - is therefore the code that
actually ships, so a test cannot pass against a simplified imitation of it.

Pages are described by ``Page``, which is a tiny HTTP response: a status, a
content type, a body, and optionally a redirect. That is enough to express
every failure class the brief lists - a Company ID wall, an employee login, a
404, a dead redirect, a vendor front door, someone else's company - and each
one is written out as the markup a real site would serve rather than as a flag
the validator is told to trust.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.careers.fetching import CareerPageFetcher, FetchOutcome, FetchResult


@dataclass(frozen=True, slots=True)
class Page:
    """One scripted HTTP response."""

    body: str = ""
    status: int = 200
    content_type: str = "text/html; charset=utf-8"
    #: When set, this URL redirects here instead of serving ``body``. The
    #: destination is looked up in the same map, so a chain works and so does a
    #: redirect into a login page or another company's site.
    redirect_to: str | None = None
    #: Simulates a transport failure: DNS, connection refused, timeout.
    unreachable: bool = False


def careers_page(company: str, *, jobs: int = 6) -> Page:
    """A believable employer careers page with real posting links."""
    listings = "\n".join(
        f'<li><a href="/careers/job/{index}00{index}">'
        f"Financial Analyst {index}</a></li>"
        for index in range(1, jobs + 1)
    )
    return Page(
        body=f"""
        <html><head><title>Careers at {company}</title></head>
        <body>
          <h1>Careers at {company}</h1>
          <form><input name="keyword" placeholder="Search jobs"></form>
          <p>Search jobs by location and team.</p>
          <ul>{listings}</ul>
        </body></html>
        """
    )


def company_id_page(vendor: str = "Paylocity") -> Page:
    """A tenant-selection wall.

    Modelled on the page that started this work, which is why it says
    "Careers" at the top: a rule that counted encouraging words before
    checking for a gate would pass it.
    """
    return Page(
        body=f"""
        <html><head><title>{vendor} Careers</title></head>
        <body>
          <h1>Careers</h1>
          <nav><a href="/jobs">Jobs</a> <a href="/careers">Careers</a></nav>
          <form>
            <label for="companyId">Company ID</label>
            <input id="companyId" name="companyId">
            <button>Continue</button>
          </form>
        </body></html>
        """
    )


def login_page(company: str = "Acme") -> Page:
    """An employee/candidate authentication wall."""
    return Page(
        body=f"""
        <html><head><title>{company} Careers - Sign In</title></head>
        <body>
          <h1>Careers</h1>
          <p>Please sign in to continue to the job portal.</p>
          <form>
            <input name="username">
            <input name="pass" type="password">
          </form>
        </body></html>
        """
    )


def marketing_page(company: str) -> Page:
    """Culture and benefits, with nothing to apply to."""
    return Page(
        body=f"""
        <html><head><title>Life at {company}</title></head>
        <body>
          <h1>Join our team</h1>
          <p>{company} is a great place to work. Our values are collaboration,
             curiosity and care. Read about our benefits, our culture and the
             communities we support. We believe in growing together and we are
             proud of the people who make {company} what it is. Learn more
             about our history, our leadership and our commitment to
             sustainability across everything we do.</p>
          <a href="/about">About us</a>
          <a href="/benefits">Benefits</a>
        </body></html>
        """
    )


def not_found_page() -> Page:
    return Page(status=404, body="<html><body><h1>Not Found</h1></body></html>")


def gone_page() -> Page:
    return Page(status=410, body="<html><body><h1>Gone</h1></body></html>")


def dead_but_two_hundred_page() -> Page:
    """The 200-with-an-apology several ATS products serve."""
    return Page(
        body="""
        <html><head><title>Job Not Found</title></head>
        <body><h1>Job Not Found</h1>
        <p>We're sorry, that job does not exist or is not currently active.</p>
        </body></html>
        """
    )


def vendor_front_door(vendor: str) -> Page:
    """The applicant-tracking product's own marketing site."""
    return Page(
        body=f"""
        <html><head><title>{vendor} - Recruiting Software</title></head>
        <body>
          <h1>{vendor}</h1>
          <p>Hiring software for growing teams. Request a demo.</p>
          <a href="/jobs/customer-success">Careers at {vendor}</a>
          <a href="/jobs/engineering">Engineering</a>
          <a href="/jobs/sales">Sales</a>
        </body></html>
        """
    )


def shell_page() -> Page:
    """A board that draws itself in the browser and serves no readable text."""
    return Page(
        body='<html><head><title></title></head><body><div id="root"></div>'
        "<noscript>You need to enable JavaScript.</noscript></body></html>"
    )


@dataclass
class FakeCareerFetcher(CareerPageFetcher):
    """``CareerPageFetcher`` with the transport replaced by a lookup table.

    ``pages`` maps a URL to what that URL serves. A URL that is not in the map
    is unreachable, which is the right default: it means a test only has to
    describe the pages it cares about, and an accidental request to anything
    else fails closed rather than silently succeeding.
    """

    pages: dict[str, Page] = field(default_factory=dict)
    #: Every URL requested, in order. Tests assert on this to show that a
    #: company's *other* observed URLs were tried before it was given up on.
    requested: list[str] = field(default_factory=list)
    #: Scripted responses for ``post_json``, used by the listing probe.
    listings: dict[str, dict] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Nothing is fetched, so robots.txt cannot be read - and asking would
        # only test the stub. Robots behaviour has its own tests.
        self.respect_robots = False

    async def __aenter__(self) -> FakeCareerFetcher:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def fetch(self, url: str) -> FetchResult:
        self.requested.append(url)
        return await self._resolve(url, url, hops=0)

    async def _resolve(self, original: str, current: str, *, hops: int) -> FetchResult:
        if hops > self.max_redirects:
            return FetchResult(
                outcome=FetchOutcome.REDIRECT_LIMIT,
                url=original,
                final_url=current,
                detail=f"More than {self.max_redirects} redirects.",
            )

        page = self.pages.get(current)
        if page is None or page.unreachable:
            return FetchResult(
                outcome=FetchOutcome.ERROR,
                url=original,
                final_url=current,
                detail="Could not reach the site (ConnectError).",
            )

        if page.redirect_to:
            return await self._resolve(original, page.redirect_to, hops=hops + 1)

        return FetchResult(
            outcome=FetchOutcome.OK,
            url=original,
            final_url=current,
            status_code=page.status,
            content_type=page.content_type,
            body=page.body,
        )

    async def post_json(self, url: str, payload: dict) -> dict | None:
        return self.listings.get(url)


__all__ = [
    "FakeCareerFetcher",
    "Page",
    "careers_page",
    "company_id_page",
    "dead_but_two_hundred_page",
    "gone_page",
    "login_page",
    "marketing_page",
    "not_found_page",
    "shell_page",
    "vendor_front_door",
]
