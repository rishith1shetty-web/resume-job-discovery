"""The shipped Workday tenant list, and how it earned its entries.

Workday publishes no cross-tenant search and no employer directory, so this
portal's coverage *is* ``DEFAULT_TENANTS`` and nothing wider. That makes the
list a data asset rather than a constant, and these tests guard the properties
that keep it trustworthy:

* every tenant was **verified live** - the host resolved, its ``robots.txt``
  returned 200 and permitted both the career site and the CXS path, and the
  site returned a posting total;
* no tenant was **inferred from a company name**. Measured across 99 plausible
  candidates, 35 resolved, and a resolving name is not the same employer: the
  ``emerson`` tenant is Emerson *College*. Names that do not resolve, and the
  collision, are pinned here as absent;
* every ``display_name`` is **the employer brand**, chosen deliberately,
  because Workday's own ``hiringOrganization.name`` is a legal entity or cost
  centre. The entities each tenant actually returned are recorded below so the
  reason is visible rather than remembered.

These tests are offline. They assert the shape and membership of the list, not
that the tenants answer today - a network assertion would make the suite fail
whenever an employer paused hiring.
"""

from __future__ import annotations

import pytest

from app.portals.adapters.workday import DEFAULT_TENANTS, WorkdayPortal, WorkdayTenant

#: The tenants the portal shipped with: ``tenant -> (wd, site, display_name)``.
#: Each was confirmed to return robots 200, an allowed CXS path and a posting
#: total on ``/wday/cxs/<tenant>/<site>/jobs``.
SHIPPED = {
    "usbank": ("1", "US_Bank_Careers", "U.S. Bank"),
    "capitalone": ("12", "Capital_One", "Capital One"),
    "cvshealth": ("1", "CVS_Health_Careers", "CVS Health"),
    "target": ("5", "targetcareers", "Target"),
    "nationwide": ("1", "Nationwide_Career", "Nationwide"),
    "allstate": ("5", "allstate_careers", "Allstate"),
    "sysco": ("5", "syscocareers", "Sysco"),
    "salesforce": ("12", "External_Career_Site", "Salesforce"),
    "nvidia": ("5", "NVIDIAExternalCareerSite", "NVIDIA"),
    "adobe": ("5", "external_experienced", "Adobe"),
    "cigna": ("5", "cignacareers", "The Cigna Group"),
    "humana": ("5", "Humana_External_Career_Site", "Humana"),
    "travelers": ("5", "External", "Travelers"),
    "pnc": ("5", "External", "PNC"),
    "ally": ("1", "Ally", "Ally"),
    "fiserv": ("5", "EXT", "Fiserv"),
    "mastercard": ("1", "CorporateCareers", "Mastercard"),
    "boeing": ("1", "EXTERNAL_CAREERS", "Boeing"),
    "3m": ("1", "Search", "3M"),
    "leidos": ("5", "External", "Leidos"),
    "amgen": ("1", "Careers", "Amgen"),
    "gilead": ("1", "gileadcareers", "Gilead Sciences"),
    "pfizer": ("1", "PfizerCareers", "Pfizer"),
    "chevron": ("5", "jobs", "Chevron"),
    "dow": ("1", "ExternalCareers", "Dow"),
    "dupont": ("5", "Jobs", "DuPont"),
    "dollartree": ("5", "dollartreeus", "Dollar Tree"),
    "workday": ("5", "Workday", "Workday"),
    "unilever": ("3", "Unilever_Experienced_Professionals", "Unilever"),
}

#: The legal entity each shipped tenant actually returned when probed. This is
#: the evidence for the ``display_name`` decision, not decoration: shipping
#: these would put "1120 Hook-SupeRx, L.L.C." on the dashboard under the name
#: CVS Health, and split one employer across several canonical keys.
OBSERVED_LEGAL_ENTITIES = {
    "usbank": "300 U.S. Bank National Association",
    "cvshealth": "1120 Hook-SupeRx, L.L.C.",
    "allstate": "National General Management Corp",
    "ally": "4001 Motors Insurance Corp.",
    "dow": "ROHM & HAAS TEXAS INC.",
    "target": "1072-28 Target India (Non-USA)",
    "sysco": "US0177 NorthEast Payroll, Division of Sysco",
    "travelers": "020 Travelers Indemnity Co",
    "nationwide": "A01 Nationwide Mutual Insurance Co.",
    "capitalone": "",
}

#: Tenant names that do **not** resolve. Every one is a real company that
#: plausibly uses Workday, and every one failed to answer on any of the eight
#: data-centre numbers probed. Listed so that adding one by inference fails
#: loudly here rather than quietly costing the portal a tenant at run time.
DOES_NOT_RESOLVE = (
    "intuit",
    "paypal",
    "netflix",
    "lowes",
    "dell",
    "nike",
    "kroger",
    "aetna",
    "progressive",
    "tmobile",
    "verizon",
    "gm",
    "ford",
    "deere",
    "abbvie",
    "moderna",
    "jnj",
    "merck",
    "exxonmobil",
)

#: Tenants that resolve and are deliberately absent, with the reason. Pinned so
#: that adding one on the strength of "it exists" has to be a decision.
RESOLVES_BUT_EXCLUDED = {
    "emerson": (
        "resolves to Emerson College, not Emerson Electric - a name collision, "
        "and the single strongest argument against inferring a tenant from a "
        "company name"
    ),
    "fedex": (
        "every US site on the tenant (FXE-US_External_Career_Site, "
        "FXF_External_Career_Site, FTN_External_Career_Site) returned zero "
        "postings when probed"
    ),
    "infosys": "tenant hosts partner brands, not an Infosys US career site",
}


def test_every_shipped_tenant_is_present_with_its_site_and_employer() -> None:
    actual = {t.tenant: (t.wd, t.site, t.display_name) for t in DEFAULT_TENANTS}

    assert actual == SHIPPED


def test_no_unverified_tenant_has_been_added_by_inference() -> None:
    """The failure mode this list invites: adding Netflix because it exists."""
    shipped = {t.tenant for t in DEFAULT_TENANTS}

    assert shipped.isdisjoint(DOES_NOT_RESOLVE)


def test_no_knowingly_excluded_tenant_has_crept_in() -> None:
    shipped = {t.tenant for t in DEFAULT_TENANTS}

    assert shipped.isdisjoint(RESOLVES_BUT_EXCLUDED)


def test_the_emerson_collision_stays_pinned() -> None:
    """``emerson`` resolves - to a university. Resolution is not identity."""
    assert "emerson" in RESOLVES_BUT_EXCLUDED
    assert "emerson" not in {t.tenant for t in DEFAULT_TENANTS}


def test_tenants_are_unique_by_tenant_and_site() -> None:
    keys = [t.key for t in DEFAULT_TENANTS]

    assert len(keys) == len(set(keys))


def test_every_tenant_names_its_employer() -> None:
    """A tenant with no display_name would publish listings from nobody.

    The payload's organisation name is a legal entity, so an unnamed tenant
    means every one of its listings reaches the dashboard under a cost centre -
    which also breaks canonical grouping, because ``build_canonical_key``
    groups on the normalised company.
    """
    for tenant in DEFAULT_TENANTS:
        assert tenant.display_name, f"{tenant.key} must name its employer"
        assert tenant.display_name == tenant.display_name.strip()


def test_no_display_name_is_a_workday_legal_entity() -> None:
    """The whole reason the name is configured, asserted rather than trusted."""
    shipped = {t.tenant: t.display_name for t in DEFAULT_TENANTS}

    for tenant, entity in OBSERVED_LEGAL_ENTITIES.items():
        assert tenant in shipped
        assert shipped[tenant] != entity


def test_no_display_name_carries_a_cost_centre_number() -> None:
    """Every observed entity was prefixed with one; no brand should be."""
    for tenant in DEFAULT_TENANTS:
        first = tenant.display_name.split()[0]
        assert not first.isdigit(), f"{tenant.key} looks like a cost centre"


def test_no_display_name_is_an_unedited_tenant_slug() -> None:
    """A Workday tenant name is usually the brand already - lowercased.

    So unlike ``lever.py``, equality with the tenant is not the smell here;
    "Target" is genuinely the employer behind the ``target`` tenant. What would
    be wrong is shipping the slug *as published* - lowercase, unpunctuated -
    because that is what reaches the dashboard and the canonical company key.
    """
    for tenant in DEFAULT_TENANTS:
        name = tenant.display_name
        assert name != name.casefold() or not name.isalpha(), (
            f"{tenant.key} looks like an unedited slug"
        )


def test_tenant_and_site_are_url_safe_path_segments() -> None:
    for tenant in DEFAULT_TENANTS:
        for segment in (tenant.tenant, tenant.site):
            assert segment == segment.strip()
            assert "/" not in segment
            assert "?" not in segment
            assert " " not in segment


def test_every_wd_number_is_a_number() -> None:
    for tenant in DEFAULT_TENANTS:
        assert tenant.wd.isdigit(), f"{tenant.key} has a non-numeric wd"


def test_the_default_list_is_what_an_unconfigured_portal_reads() -> None:
    assert WorkdayPortal().tenants == DEFAULT_TENANTS


def test_configuration_replaces_the_defaults_entirely() -> None:
    """Adding an employer must never require a deployment."""
    portal = WorkdayPortal(
        {
            "tenants": [
                {"tenant": "acme", "wd": "5", "site": "Careers", "display_name": "Acme"}
            ]
        }
    )

    assert portal.tenants == (WorkdayTenant("acme", "5", "Careers", "Acme"),)


@pytest.mark.parametrize("tenant", DEFAULT_TENANTS, ids=lambda t: t.key)
def test_each_tenant_builds_only_permitted_urls(tenant: WorkdayTenant) -> None:
    host = f"https://{tenant.tenant}.wd{tenant.wd}.myworkdayjobs.com"

    assert tenant.host == host
    assert tenant.jobs_url == f"{host}/wday/cxs/{tenant.tenant}/{tenant.site}/jobs"
    assert tenant.site_url == f"{host}/{tenant.site}/"
    # The path every tenant's robots.txt closes, on every tenant probed.
    for url in (tenant.jobs_url, tenant.site_url):
        assert "/refreshFacet/" not in url
        assert "/talentcommunity/" not in url


def test_the_shipped_count_is_what_was_actually_verified() -> None:
    """Thirty were probed; FedEx returned nothing and was dropped."""
    assert len(DEFAULT_TENANTS) == 29


# --- Depth -----------------------------------------------------------------


def test_workdays_seed_carries_its_own_depth() -> None:
    """Set for this portal alone, through the row SearchDepth reads."""
    from app.services.portal_seed import PORTAL_SEEDS

    seed = next(s for s in PORTAL_SEEDS if s.slug == "workday")

    assert dict(seed.config_options) == {
        "query_timeout_seconds": 150.0,
        "max_runtime_seconds": 300.0,
    }


def test_no_other_portal_seed_carries_depth_options() -> None:
    """A per-portal escape hatch, not a way to tune portals generally."""
    from app.services.portal_seed import PORTAL_SEEDS

    for seed in PORTAL_SEEDS:
        if seed.slug == "workday":
            continue
        assert not seed.config_options, seed.slug


def test_the_shared_depth_defaults_are_untouched() -> None:
    """Raising Workday's ceiling must not raise anybody else's."""
    from app.search.depth import (
        DEFAULT_MAX_RUNTIME_SECONDS,
        DEFAULT_QUERY_TIMEOUT_SECONDS,
        SearchDepth,
    )

    assert DEFAULT_QUERY_TIMEOUT_SECONDS == 90.0
    assert DEFAULT_MAX_RUNTIME_SECONDS == 150.0
    assert SearchDepth().query_timeout_seconds == 90.0
    assert SearchDepth().max_runtime_seconds == 150.0


def test_workdays_depth_is_what_a_cold_pass_actually_needs() -> None:
    """The measurement behind the numbers, so they are not folklore.

    A cold pass over the shipped tenants costs about 140 requests and measured
    86-94s; the leg budget must then leave room for the freshness ladder to
    widen past 24 hours, which needs no further requests at all.
    """
    from app.search.depth import SearchDepth
    from app.services.portal_seed import PORTAL_SEEDS

    seed = next(s for s in PORTAL_SEEDS if s.slug == "workday")
    depth = SearchDepth.from_options(dict(seed.config_options))

    assert depth.query_timeout_seconds > 94.0
    assert depth.max_runtime_seconds >= 2 * depth.query_timeout_seconds
