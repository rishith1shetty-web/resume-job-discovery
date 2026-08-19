"""Taxonomy fan-out: reading a role's own pages rather than only one of them.

Hiring Cafe and Jobright both publish a role as a base slug plus per-place
variants, and neither surface may be paged - ``?page=`` is disallowed on one
and ignored by the other. So the only legitimate way to collect more of a
portal is to read more of the role's own pages.

Two properties matter and are pinned here for both adapters:

* **Breadth.** Several same-role slugs are read, duplicates across them
  collapse, and one page failing costs only that page.
* **Containment.** Every slug read begins with the matched base slug, so the
  fan-out can widen *where* a role is searched but never *which* role - the
  guarantee in CLAUDE.md section 3. A slug that merely shares the prefix but
  names a different job (``financial-analyst-manager``) is never read.

The two adapters deliberately differ in how far they fan out, because the
portals differ: Hiring Cafe's location pages carry distinct listings, while
Jobright's serve one shared list. That evidence is recorded in each adapter's
docstring, and the differing defaults are asserted below.

No test in this file touches the network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.constants import FreshnessWindow
from app.portals.adapters import hiringcafe as hc_module
from app.portals.adapters import jobright as jr_module
from app.portals.adapters.hiringcafe import HiringCafePortal
from app.portals.adapters.jobright import JobrightPortal
from app.portals.base import PortalSearchRequest
from app.portals.exceptions import PortalTimeoutError
from app.processing.us_locations import is_us_location_slug

FIXTURES = Path(__file__).parent / "fixtures" / "portals"

BASE = "financial-analyst"

#: Same prefix as the base role, but a different job. Never collected.
ROLE_MODIFIER = f"{BASE}-manager"


def slug_of(url: str) -> str:
    return url.rsplit("/", 1)[-1]


# --- The shared location test ------------------------------------------------


@pytest.mark.parametrize(
    "segment",
    ["austin-tx", "new-york-ny", "california", "new-york", "portland-or"],
)
def test_a_us_place_is_recognised(segment: str) -> None:
    assert is_us_location_slug(segment) is True


@pytest.mark.parametrize(
    "segment", ["manager", "lead", "ii", "", "senior-manager", "remote"]
)
def test_anything_that_is_not_a_place_is_refused(segment: str) -> None:
    """A false positive here would widen the search to another occupation."""
    assert is_us_location_slug(segment) is False


# --- Hiring Cafe -------------------------------------------------------------

HC_VARIANTS = (f"{BASE}-austin-tx", f"{BASE}-california", f"{BASE}-chicago-il")
HC_TAXONOMY = frozenset({BASE, *HC_VARIANTS, ROLE_MODIFIER, "data-analyst"})


@pytest.fixture
def hc_payload() -> dict:
    return json.loads(
        (FIXTURES / "hiringcafe_financial_analyst.json").read_text(encoding="utf-8")
    )


@pytest.fixture
def hc_adapter() -> HiringCafePortal:
    return HiringCafePortal(options={"rate_limit_per_minute": 0})


def hc_page(payload: dict, slug: str | None) -> str:
    """The fixture page, optionally given a slug-specific set of identities."""
    clone = json.loads(json.dumps(payload))
    if slug is not None:
        for index, hit in enumerate(clone["props"]["pageProps"]["ssrHits"]):
            hit["id"] = f"{slug}-{index}"
    return (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(clone)
        + "</script></body></html>"
    )


@pytest.fixture
def hc_serve(monkeypatch: pytest.MonkeyPatch, hc_payload: dict):
    """Install a fake fetch; returns the list of URLs it was asked for."""

    def install(*, distinct: bool = True, fail: set[str] = frozenset()) -> list[str]:
        calls: list[str] = []

        async def _taxonomy(self):  # noqa: ANN001, ANN202
            return HC_TAXONOMY

        async def _fetch(self, url, **kwargs):  # noqa: ANN001, ANN202
            calls.append(url)
            slug = slug_of(url)
            if slug in fail:
                raise PortalTimeoutError(f"{slug} timed out", portal="hiringcafe")
            return hc_page(hc_payload, slug if distinct else None)

        monkeypatch.setattr(HiringCafePortal, "_load_taxonomy", _taxonomy)
        monkeypatch.setattr("app.portals.http.PortalHttpClient.get_text", _fetch)
        return calls

    return install


def hc_request(title: str = "Financial Analyst") -> PortalSearchRequest:
    return PortalSearchRequest(
        queries=(title,), window=FreshnessWindow.LAST_7D, limit=500
    )


async def test_hiringcafe_reads_the_base_slug_and_its_location_variants(
    hc_adapter: HiringCafePortal, hc_serve
) -> None:
    calls = hc_serve()

    await hc_adapter.search(hc_request())

    read = [slug_of(url) for url in calls]
    assert read[0] == BASE, "the role's own page is read first"
    assert set(read) == {BASE, *HC_VARIANTS}


async def test_hiringcafe_never_reads_a_role_modifier_as_a_location(
    hc_adapter: HiringCafePortal, hc_serve
) -> None:
    calls = hc_serve()

    await hc_adapter.search(hc_request())

    assert ROLE_MODIFIER not in [slug_of(url) for url in calls]


async def test_hiringcafe_location_variants_contribute_new_listings(
    hc_adapter: HiringCafePortal, hc_serve
) -> None:
    hc_serve()

    everything = await hc_adapter.search(hc_request())
    only_base = await HiringCafePortal(
        options={"rate_limit_per_minute": 0, "max_slugs": 1}
    ).search(hc_request())

    assert only_base.jobs
    assert len(everything.jobs) > len(only_base.jobs)
    # Four pages of distinct listings, not one page counted four times.
    assert len(everything.jobs) == len(only_base.jobs) * 4


async def test_hiringcafe_collapses_a_vacancy_listed_under_two_places(
    hc_adapter: HiringCafePortal, hc_serve
) -> None:
    hc_serve(distinct=False)

    result = await hc_adapter.search(hc_request())

    ids = [job.external_id for job in result.jobs]
    assert ids
    assert len(ids) == len(set(ids))


async def test_hiringcafe_fan_out_adds_no_disallowed_parameter(
    hc_adapter: HiringCafePortal, hc_serve
) -> None:
    """``?page=`` and ``?searchState=`` stay disallowed at any slug count."""
    calls = hc_serve()

    await hc_adapter.search(hc_request())

    assert calls
    for url in calls:
        assert "?" not in url, url
        assert "page=" not in url, url
        assert "searchState" not in url, url


async def test_hiringcafe_keeps_results_when_one_slug_fails(
    hc_adapter: HiringCafePortal, hc_serve
) -> None:
    hc_serve(fail={HC_VARIANTS[0], HC_VARIANTS[1]})

    result = await hc_adapter.search(hc_request())

    assert result.jobs
    sources = {job.source_url for job in result.jobs}
    assert f"{hc_module.BASE_URL}/jobs/{BASE}" in sources
    assert f"{hc_module.BASE_URL}/jobs/{HC_VARIANTS[0]}" not in sources


async def test_hiringcafe_reports_a_portal_error_when_every_slug_fails(
    hc_adapter: HiringCafePortal, hc_serve
) -> None:
    """Returning nothing would read as "this role has no matching jobs"."""
    hc_serve(fail={BASE, *HC_VARIANTS})

    with pytest.raises(PortalTimeoutError):
        await hc_adapter.search(hc_request())


async def test_hiringcafe_fetches_each_slug_once_per_run(
    hc_adapter: HiringCafePortal, hc_serve
) -> None:
    """A run asks per title variation, and those resolve to the same pages."""
    calls = hc_serve()

    for title in ("Financial Analyst", "Senior Financial Analyst", "Financial Analyst"):
        await hc_adapter.search(hc_request(title))

    assert len(calls) == 4
    assert len(set(calls)) == 4


async def test_hiringcafe_slug_budget_is_configurable(hc_serve) -> None:
    calls = hc_serve()
    bounded = HiringCafePortal(options={"rate_limit_per_minute": 0, "max_slugs": 2})

    await bounded.search(hc_request())

    assert len(calls) == 2
    assert slug_of(calls[0]) == BASE


def test_hiringcafe_single_slug_matching_is_unchanged() -> None:
    """The existing contract still holds; the fan-out is layered on top."""
    assert HiringCafePortal._match_slug("Financial Analyst", HC_TAXONOMY) == BASE
    assert HiringCafePortal._match_slug("Senior Financial Analyst", HC_TAXONOMY) == BASE
    assert HiringCafePortal._match_slug("Marine Biologist", HC_TAXONOMY) is None


def test_hiringcafe_a_role_without_variants_reads_only_its_own_page() -> None:
    assert HiringCafePortal._match_slugs(
        "Data Analyst", HC_TAXONOMY, max_slugs=10
    ) == ("data-analyst",)


# --- Jobright ----------------------------------------------------------------
#
# Jobright fans out far less, and deliberately: its location pages were measured
# serving one shared list, so reading all twenty-four would cost twenty-two
# requests for no extra listings. What it does need is the *corporate* slug,
# which the taxonomy spells "corporate-finance-analyst".

JR_CORPORATE = "corporate-finance-analyst"
JR_VARIANTS = (f"{BASE}-in-austin-tx", f"{BASE}-in-new-york-ny")
JR_TAXONOMY = frozenset({BASE, JR_CORPORATE, *JR_VARIANTS, ROLE_MODIFIER})


@pytest.fixture
def jr_payload() -> dict:
    return json.loads(
        (FIXTURES / "jobright_financial_analyst.json").read_text(encoding="utf-8")
    )


@pytest.fixture
def jr_adapter() -> JobrightPortal:
    portal = JobrightPortal(options={"rate_limit_per_minute": 0})
    type(portal)._taxonomy_cache = JR_TAXONOMY
    return portal


def jr_page(payload: dict, slug: str | None) -> str:
    clone = json.loads(json.dumps({"props": payload["props"]}))
    if slug is not None:
        for index, job in enumerate(clone["props"]["pageProps"]["jobList"]):
            # The identity Jobright addresses a listing by lives on jobResult.
            job["jobResult"]["jobId"] = f"{slug}-{index}"
    return (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(clone)
        + "</script></body></html>"
    )


def jr_serve(
    adapter: JobrightPortal,
    payload: dict,
    *,
    distinct: bool = True,
    fail: set[str] = frozenset(),
) -> list[str]:
    calls: list[str] = []

    async def _fetch(url, **_kwargs):  # noqa: ANN001, ANN202
        calls.append(url)
        slug = slug_of(url)
        if slug in fail:
            raise PortalTimeoutError(f"{slug} timed out", portal="jobright")
        return jr_page(payload, slug if distinct else None)

    adapter._http.get_text = _fetch  # type: ignore[method-assign]
    return calls


def jr_request(title: str) -> PortalSearchRequest:
    return PortalSearchRequest(
        queries=(title,), window=FreshnessWindow.LAST_7D, limit=500
    )


def test_jobright_reaches_the_taxonomys_own_spelling_of_corporate() -> None:
    """"Corporate Financial Analyst" must not degrade to the plain base role.

    The taxonomy carries ``corporate-finance-analyst``. Without the wording
    variant the left-trim fallback drops "corporate" and searches the less
    specific page, losing that page's distinct listings.
    """
    assert (
        JobrightPortal._match_slug("Corporate Financial Analyst", JR_TAXONOMY)
        == JR_CORPORATE
    )


def test_jobright_reads_the_base_slug_and_a_single_location_variant() -> None:
    """One variant only: the rest were measured serving the same list."""
    slugs = JobrightPortal._match_slugs(
        "Financial Analyst", JR_TAXONOMY, max_slugs=jr_module.MAX_SLUGS_PER_ROLE
    )

    assert slugs[0] == BASE
    assert len(slugs) == 2
    assert slugs[1].startswith(f"{BASE}-in-")


def test_jobright_does_not_fan_out_as_widely_as_hiringcafe() -> None:
    """The defaults differ because the portals were measured behaving differently."""
    assert jr_module.MAX_SLUGS_PER_ROLE < hc_module.MAX_SLUGS_PER_ROLE


def test_jobright_never_reads_a_role_modifier_as_a_location() -> None:
    assert ROLE_MODIFIER not in JobrightPortal._match_slugs(
        "Financial Analyst", JR_TAXONOMY, max_slugs=10
    )


async def test_jobright_queries_several_targeted_slugs(
    jr_adapter: JobrightPortal, jr_payload: dict
) -> None:
    calls = jr_serve(jr_adapter, jr_payload)

    await jr_adapter.search(
        PortalSearchRequest(
            queries=("Financial Analyst", "Corporate Financial Analyst"),
            window=FreshnessWindow.LAST_7D,
            limit=500,
        )
    )

    read = {slug_of(url) for url in calls}
    assert BASE in read
    assert JR_CORPORATE in read


async def test_jobright_corporate_slug_contributes_distinct_records(
    jr_adapter: JobrightPortal, jr_payload: dict
) -> None:
    jr_serve(jr_adapter, jr_payload)

    base_only = await jr_adapter.search(jr_request("Financial Analyst"))
    base_ids = {job.external_id for job in base_only.jobs}

    fresh = JobrightPortal(options={"rate_limit_per_minute": 0})
    type(fresh)._taxonomy_cache = JR_TAXONOMY
    jr_serve(fresh, jr_payload)
    corporate = await fresh.search(jr_request("Corporate Financial Analyst"))
    corporate_ids = {job.external_id for job in corporate.jobs}

    assert corporate_ids
    assert not (corporate_ids & base_ids), "the corporate page is a distinct set"


async def test_jobright_deduplicates_records_across_slugs(
    jr_adapter: JobrightPortal, jr_payload: dict
) -> None:
    jr_serve(jr_adapter, jr_payload, distinct=False)

    result = await jr_adapter.search(jr_request("Financial Analyst"))

    ids = [job.external_id for job in result.jobs]
    assert ids
    assert len(ids) == len(set(ids))


async def test_jobright_a_failed_secondary_slug_keeps_the_primary_results(
    jr_adapter: JobrightPortal, jr_payload: dict
) -> None:
    jr_serve(jr_adapter, jr_payload, fail=set(JR_VARIANTS))

    result = await jr_adapter.search(jr_request("Financial Analyst"))

    assert result.jobs, "the base slug answered and must not be discarded"


async def test_jobright_reports_a_portal_error_when_every_slug_fails(
    jr_adapter: JobrightPortal, jr_payload: dict
) -> None:
    jr_serve(jr_adapter, jr_payload, fail={BASE, *JR_VARIANTS})

    with pytest.raises(PortalTimeoutError):
        await jr_adapter.search(jr_request("Financial Analyst"))


async def test_jobright_fetches_each_slug_once_per_run(
    jr_adapter: JobrightPortal, jr_payload: dict
) -> None:
    calls = jr_serve(jr_adapter, jr_payload)

    for title in ("Financial Analyst", "Senior Financial Analyst"):
        await jr_adapter.search(jr_request(title))

    assert len(calls) == len(set(calls))


async def test_jobright_never_calls_the_disallowed_api_surface(
    jr_adapter: JobrightPortal, jr_payload: dict
) -> None:
    calls = jr_serve(jr_adapter, jr_payload)

    await jr_adapter.search(jr_request("Financial Analyst"))

    assert calls
    for url in calls:
        assert "/api/" not in url, url
        assert "page=" not in url, url
