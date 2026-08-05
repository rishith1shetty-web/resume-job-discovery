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

        existing = await session.scalar(
            select(Portal)
            .where(Portal.slug == seed.slug)
            .options(selectinload(Portal.config))
        )

        if existing is None:
            portal = Portal(
                slug=seed.slug,
                display_name=adapter.display_name if adapter else seed.slug.title(),
                website_url=seed.website_url,
                adapter_key=seed.slug if adapter else None,
                is_enabled=seed.is_enabled,
                # True only when an adapter is actually registered.
                is_implemented=adapter is not None,
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
            existing.adapter_key = seed.slug if adapter else existing.adapter_key
            existing.is_implemented = adapter is not None
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
