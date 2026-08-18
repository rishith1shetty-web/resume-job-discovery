"""Register portal rows for the adapters that exist.

The portal list is data, not code: this seeds rows so the dashboard and the
search service have something to resolve, and it derives everything it can from
the adapter's own declared capabilities rather than restating them here.

Idempotent. Running it twice changes nothing, and it never enables a portal the
operator has since disabled.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
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
        slug="ziprecruiter",
        website_url="https://www.ziprecruiter.com/",
        display_order=17,
        # Disabled deliberately, and not for an access reason: the adapter is
        # permitted to collect and does. It is disabled because the surface it
        # is permitted to read publishes no posting description, so every
        # listing is rejected as unscoreable and the portal would take a share
        # of every search's quota while returning nothing. Flip this to True to
        # select it anyway.
        is_enabled=False,
        notes=(
            "Collects through the job-search MCP endpoint that ZipRecruiter "
            "publishes in its own /llms.txt manifest (read only, no auth). "
            "Its robots.txt is deny-by-default for this client - "
            "'User-agent: * / Allow: /llms.txt / Disallow: /' - so no "
            "ziprecruiter.com page is ever requested. Disabled because that "
            "endpoint returns no job description: title, company, location, "
            "salary, benefits and age only. The matching engine rejects a "
            "posting it cannot read, so the portal currently qualifies no "
            "jobs. Enabling it needs a description source, not a code change."
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
    PortalSeed(
        slug="linkedin",
        website_url="https://www.linkedin.com/",
        display_order=20,
        # Disabled deliberately: robots.txt refuses automated access.
        is_enabled=False,
        notes=(
            "Disabled: LinkedIn's robots.txt disallows automated access for "
            "this client (User-agent: * / Disallow: /). The adapter is "
            "implemented and tested but will not collect until an operator "
            "who holds LinkedIn's permission sets the portal option "
            "'access_authorized' to true. Easy Apply listings are excluded "
            "when it does run."
        ),
    ),
)


async def ensure_portals_seeded(
    session: AsyncSession, *, seeds: tuple[PortalSeed, ...] = PORTAL_SEEDS
) -> dict[str, str]:
    """Create or refresh the portal rows. Returns slug -> action taken."""
    actions: dict[str, str] = {}

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
