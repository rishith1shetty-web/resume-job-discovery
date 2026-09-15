"""Register portal rows for the adapters that exist.

The portal list is data, not code: this seeds rows so the dashboard and the
search service have something to resolve, and it derives everything it can from
the adapter's own declared capabilities rather than restating them here.

Idempotent. Running it twice changes nothing, and it never enables a portal the
operator has since disabled.

It also removes portals the product has retired - see ``RETIRED_PORTAL_SLUGS``.
Dropping a seed alone would leave the row behind on every existing install.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.careers.base import career_registry
from app.core.logging import get_logger
from app.models.portal import Portal, PortalConfig
from app.portals.registry import registry

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PortalSeed:
    """Facts about a portal that its adapter cannot supply itself."""

    slug: str
    website_url: str
    display_order: int
    #: Whether collection may run. False where access is not authorised.
    is_enabled: bool
    notes: str


#: The portals with adapters (docs/DECISIONS.md D-001, plus Hiring Cafe in
#: Phase 9A). Adding a portal here is not what makes it usable - it still needs
#: a registered adapter.
PORTAL_SEEDS: tuple[PortalSeed, ...] = (
    PortalSeed(
        slug="jobright",
        website_url="https://jobright.ai/",
        display_order=10,
        is_enabled=True,
        notes=(
            "Collects from the robots-allowed /jobs/<taxonomy> pages. The "
            "private /api/ surface is disallowed by robots.txt and is not "
            "used. Those pages do not paginate, so roughly 16 listings are "
            "available per taxonomy slug per pass."
        ),
    ),
    PortalSeed(
        slug="hiringcafe",
        website_url="https://hiringcafe.com/",
        display_order=15,
        is_enabled=True,
        notes=(
            "Collects from the robots-allowed /jobs/<taxonomy> pages listed in "
            "the site's own jobs-sitemap.xml. The site's search surface "
            "(?searchState=) and pagination (?page=) are both disallowed by "
            "robots.txt and are not used, so roughly 20 listings are available "
            "per taxonomy slug per pass. Descriptions are composed from Hiring "
            "Cafe's structured fields, not the employer's verbatim posting; "
            "application links point at the employer's own ATS."
        ),
    ),
    PortalSeed(
        slug="greenhouse",
        website_url="https://boards-api.greenhouse.io/",
        display_order=18,
        is_enabled=True,
        notes=(
            "Collects from Greenhouse's public, documented, no-auth Job Board "
            "API (/v1/boards/<token>/jobs?content=true); its robots.txt "
            "disallows only /embed/, which is never touched. Greenhouse is an "
            "ATS, not a job board: it publishes NO cross-company search, so "
            "coverage is exactly the employers listed in this portal's "
            "board_tokens option and no wider. Add or remove employers there "
            "without a deployment. Each board returns the employer's complete "
            "posting text in one request, and application links point at the "
            "employer's own careers page where they host one."
        ),
    ),
    PortalSeed(
        slug="simplyhired",
        website_url="https://www.simplyhired.com/",
        display_order=16,
        is_enabled=True,
        notes=(
            "Collects from the robots-allowed /search pages and reads full "
            "posting text from /job/<jobKey>. The private /c/jobs-api/ "
            "surface and the /out?r= click redirect are both disallowed by "
            "robots.txt and are not used, so listings link to the site's own "
            "crawler-facing job URL. Real cursor pagination, capped at two "
            "pages per query. Descriptions used for scoring are composed from "
            "the search page's structured fields; the employer's full text is "
            "fetched on demand."
        ),
    ),
    PortalSeed(
        slug="scalejobs",
        website_url="https://scale.jobs/",
        display_order=19,
        is_enabled=True,
        notes=(
            "Collects from the public, self-documented, no-auth search "
            "endpoint on jobhose-prod.scale.jobs, which is the host the site's "
            "own client calls with userId=public-search. That host serves no "
            "robots.txt; the scale.jobs site host is a different origin whose "
            "robots.txt disallows /api/, and nothing on it is requested - its "
            "job pages are client-rendered shells carrying no listing anyway. "
            "excludeLinkedIn=true is sent on every request because this "
            "project does not collect LinkedIn, and Easy Apply listings stay "
            "excluded by the endpoint's own default. Only read-only GETs to "
            "/api/search-jobs are issued; the host's administrative sync and "
            "cron endpoints are never called. Real date filtering and real "
            "pagination. Descriptions are the publisher's model-written "
            "summaries, not the employer's verbatim posting; application "
            "links point at the employer's own ATS."
        ),
    ),
    PortalSeed(
        slug="dayforce",
        website_url="https://jobs.dayforcehcm.com/",
        display_order=21,
        is_enabled=True,
        notes=(
            "Collects from the public candidate-portal search API "
            "(POST /api/geo/<clientNamespace>/jobposting/search). No account "
            "and no API key: the endpoint is guarded by an ordinary "
            "double-submit CSRF token read from the public /api/auth/csrf, "
            "which is why it is the one adapter that keeps a cookie session. "
            "This host's robots.txt carries no User-agent group and no "
            "Disallow, so no path is closed to this client. Dayforce is an "
            "ATS, not a job board: it publishes NO cross-company search, so "
            "coverage is exactly the employers listed in this portal's boards "
            "option and no wider - mydayforce/alljobs is Dayforce's own "
            "corporate board, not an aggregate. The API also does not "
            "paginate: every paging parameter is ignored and at most 25 "
            "postings come back per query, so breadth comes from querying each "
            "board once per same-role title variation. Each posting carries "
            "the employer's full text, a real publication timestamp and "
            "structured locations with ISO country codes; the employer name "
            "comes from the board's configuration entry, because the payload "
            "carries none."
        ),
    ),
    PortalSeed(
        slug="google_careers",
        website_url="https://www.google.com/about/careers/",
        display_order=22,
        is_enabled=True,
        notes=(
            "Collects from Google's own careers search page, which renders its "
            "results server-side: the listings arrive inside the page's "
            "AF_initDataCallback data island, so there is no private API to "
            "call. No account, no API key, no cookie and no token; the request "
            "goes out under this project's own user agent, which Google serves "
            "identically to a browser's. robots.txt permits the results page "
            "and the individual job pages for this client but disallows "
            "pagination (?page=) four separate ways, so only page one is read - "
            "20 listings per query - and breadth comes from querying once per "
            "same-role title variation. Coverage is one employer: Google and "
            "the organisations hiring through its careers site, including "
            "DeepMind and YouTube. The payload carries three timestamps; "
            "posted_at is taken from the publication one, NOT from the refresh "
            "timestamp the site's own sort_by=date orders on, because listings "
            "published months ago are routinely refreshed to the top of that "
            "ordering. Each record carries the employer's full posting text - "
            "about, responsibilities and qualifications - and structured "
            "locations with ISO country codes."
        ),
    ),
    PortalSeed(
        slug="lever",
        website_url="https://api.lever.co/v0/postings/",
        display_order=23,
        is_enabled=True,
        notes=(
            "Collects from Lever's public, no-auth postings feed "
            "(/v0/postings/<board>?mode=json). api.lever.co's robots.txt "
            "allows everything with Crawl-delay: 1; the authenticated "
            "/v1/postings API answers 401 and is never touched. Lever is an "
            "ATS, not a job board: it publishes NO cross-company search - "
            "there is no board index, sitemap or directory to enumerate - so "
            "coverage is exactly the employers listed in this portal's boards "
            "option and no wider. Add or remove employers there without a "
            "deployment; every default board was confirmed live and its "
            "employer name read from that board's own schema.org "
            "hiringOrganization, because the feed itself carries no company "
            "name and a slug must never be turned into one. The feed's "
            "?query=, ?search= and ?workplaceType= parameters are silently "
            "ignored - they return the whole board - so none is sent, and "
            "title filtering happens locally against the same-role "
            "variations. One request returns a board in full, so there is no "
            "paging. Each posting carries the employer's full text already "
            "split into named sections, an ISO country code, an explicit "
            "remote/hybrid/onsite value, and createdAt - the only timestamp "
            "in the feed, verified against Lever's own schema.org datePosted "
            "as the original publication date and never a refresh."
        ),
    ),
    PortalSeed(
        slug="ashby",
        website_url="https://jobs.ashbyhq.com/",
        display_order=24,
        is_enabled=True,
        notes=(
            "Collects from Ashby's public, server-rendered board pages "
            "(/<board> and /<board>/<job>), reading the page's own "
            "window.__appData island and each job's schema.org JobPosting. "
            "jobs.ashbyhq.com/robots.txt allows those paths and disallows "
            "/api/, /b/ and /meeting/, none of which is touched. The "
            "one-request posting API on api.ashbyhq.com is deliberately NOT "
            "used: that host answers 401 for robots.txt, and this project "
            "treats a robots policy it cannot read as a refusal, so the "
            "slower permitted path is taken rather than opening an exception "
            "for one portal. Ashby is an ATS, not a job board: it publishes "
            "NO cross-company search and no board directory, so coverage is "
            "exactly the employers listed in this portal's boards option and "
            "no wider. Employer names are read from the board page itself, so "
            "a board is configured by slug alone and cannot carry a stale "
            "company name. A slug that does not exist returns HTTP 200 with "
            "an empty shell, so a board counts as real only when the page "
            "carries an organisation and a posting list. Titles are filtered "
            "locally against the same-role variations and only matching jobs "
            "have their pages opened, so a board of a hundred postings "
            "normally costs one request. posted_at comes from datePosted, "
            "the original publication date - Ashby publishes no update or "
            "refresh timestamp."
        ),
    ),
    PortalSeed(
        slug="career_pages",
        website_url="https://www.rising-path.example/careers",
        display_order=25,
        is_enabled=True,
        notes=(
            "Not a job board: returns companies and the official career page "
            "each one publishes, rather than individual vacancies. Companies "
            "are discovered from openings that are live right now - reusing "
            "the listings the other selected portals already returned, or "
            "collecting shallowly through the enabled portals when Career "
            "Pages is selected on its own. No company list is maintained "
            "anywhere: an employer appears only by having a current US opening "
            "that passes the same role gate the job pipeline uses. Career page "
            "URLs are accepted only when they verify as the employer's own "
            "domain or the employer's own space on an applicant-tracking "
            "system; aggregators and job boards are refused. Results are "
            "capped at 20 per run and take no part in the Find 20/50/100 job "
            "allocation."
        ),
    ),
)


#: Portals that were once seeded and must no longer exist (D-005).
#:
#: Dropping a slug from ``PORTAL_SEEDS`` stops a *fresh* database from growing
#: the row, but it does nothing to an installation that already has one - and
#: ``GET /portals`` selects every row, so a retired portal would keep appearing
#: in Find Matches forever. Listing it here is what actually removes it.
#:
#: Deliberately not a migration: migrations carry schema, and portal rows are
#: environment data owned by this script (see app/scripts/seed_portals.py).
RETIRED_PORTAL_SLUGS: frozenset[str] = frozenset({"linkedin", "ziprecruiter"})


async def _retire_portals(
    session: AsyncSession, slugs: frozenset[str]
) -> dict[str, str]:
    """Delete rows for portals that are no longer part of the product.

    Refuses to delete a portal that any job or search run still references.
    Removing it would cascade into ``job_sources`` and take real discovery and
    application history with it, and losing a user's history is a worse outcome
    than an unused row lingering. Such a portal is reported instead, so an
    operator can decide, rather than being silently kept or silently destroyed.
    """
    from app.models.job import JobSource
    from app.models.search import SearchRunPortal

    actions: dict[str, str] = {}

    for slug in sorted(slugs):
        portal = await session.scalar(select(Portal).where(Portal.slug == slug))
        if portal is None:
            continue

        referencing = await session.scalar(
            select(func.count())
            .select_from(JobSource)
            .where(JobSource.portal_id == portal.id)
        ) or 0
        referencing += await session.scalar(
            select(func.count())
            .select_from(SearchRunPortal)
            .where(SearchRunPortal.portal_id == portal.id)
        ) or 0

        if referencing:
            logger.warning(
                "retired_portal_still_referenced",
                slug=slug,
                references=referencing,
            )
            actions[slug] = f"kept ({referencing} references)"
            continue

        await session.delete(portal)
        actions[slug] = "removed"

    return actions


async def ensure_portals_seeded(
    session: AsyncSession,
    *,
    seeds: tuple[PortalSeed, ...] = PORTAL_SEEDS,
    retired: frozenset[str] = RETIRED_PORTAL_SLUGS,
) -> dict[str, str]:
    """Create or refresh the portal rows. Returns slug -> action taken."""
    actions: dict[str, str] = await _retire_portals(session, retired)

    for seed in seeds:
        adapter = registry.get(seed.slug) if registry.is_registered(seed.slug) else None
        capabilities = adapter(options={}).capabilities if adapter else None

        # A career source is implemented, and selectable, without being a
        # portal adapter: it answers "which companies are hiring" rather than
        # returning listings, so it has no PortalCapabilities to declare and
        # every capability flag stays False - which is accurate, not a
        # placeholder. Without this the row would seed as unimplemented and the
        # dashboard would never offer its checkbox.
        career_source = (
            career_registry.get(seed.slug)
            if career_registry.is_registered(seed.slug)
            else None
        )
        implemented = adapter is not None or career_source is not None
        display_name = (
            adapter.display_name
            if adapter
            else career_source.display_name
            if career_source
            else seed.slug.title()
        )

        existing = await session.scalar(
            select(Portal)
            .where(Portal.slug == seed.slug)
            .options(selectinload(Portal.config))
        )

        if existing is None:
            portal = Portal(
                slug=seed.slug,
                display_name=display_name,
                website_url=seed.website_url,
                adapter_key=seed.slug if implemented else None,
                is_enabled=seed.is_enabled,
                # True only when something is actually registered to run it.
                is_implemented=implemented,
                supports_date_filter=bool(
                    capabilities and capabilities.supports_date_filter
                ),
                supports_description_fetch=bool(
                    capabilities and capabilities.supports_description_fetch
                ),
                requires_auth=bool(capabilities and capabilities.requires_auth),
                notes=seed.notes,
                display_order=seed.display_order,
            )
            session.add(portal)
            await session.flush()
            session.add(PortalConfig(portal_id=portal.id))
            actions[seed.slug] = "created"
        else:
            # Refresh what the adapter owns; leave is_enabled alone so an
            # operator's decision to disable a portal is not undone by a
            # redeploy.
            existing.adapter_key = seed.slug if implemented else existing.adapter_key
            existing.is_implemented = implemented
            existing.website_url = seed.website_url
            existing.notes = seed.notes
            existing.display_order = seed.display_order
            if capabilities is not None:
                existing.supports_date_filter = capabilities.supports_date_filter
                existing.supports_description_fetch = (
                    capabilities.supports_description_fetch
                )
                existing.requires_auth = capabilities.requires_auth
            if existing.config is None:
                session.add(PortalConfig(portal_id=existing.id))
            actions[seed.slug] = "updated"

    await session.flush()
    logger.info("portals_seeded", actions=actions)
    return actions
