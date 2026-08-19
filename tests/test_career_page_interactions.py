"""Career pages get the same persistent Applied/Skipped lifecycle jobs have.

A career page is a company's recruiting site, not a vacancy. So the decision
recorded here means "I have dealt with this employer's page" and nothing more:

* applying to a career page does **not** apply to the jobs listed on it;
* skipping one does **not** skip that company's individual jobs.

Both are asserted below rather than left to the docstrings that claim them.

The rest mirrors the job contract exactly - resume-scoped, mutually exclusive,
idempotent, suppressed from later runs server-side, visible in history - and
these tests are written against the API and the search pipeline rather than the
service, so they fail if any layer between the two stops honouring it.
"""

from __future__ import annotations

import uuid
from typing import Any

from httpx import AsyncClient, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.careers.identity import career_page_key
from app.core.config import Settings
from app.core.constants import ApplicationStatus
from app.models.application import Application
from app.models.career_page import CareerPageInteraction
from app.models.user import User
from app.services import career_page_service
from app.services.user_service import get_or_create_default_user
from tests.factories import make_job, make_resume

DECISION = "/api/v1/career-pages/decision"
STATE = "/api/v1/career-pages/state"
LIST = "/api/v1/career-pages"

CAPITAL_ONE = "https://www.capitalonecareers.com/"
BOOZ_ALLEN = "https://careers.boozallen.com/"
COGNIZANT = "https://careers.cognizant.com/global/en"


async def _acting_user(session: AsyncSession, settings: Settings) -> User:
    return await get_or_create_default_user(session, settings)


def _page(
    url: str = CAPITAL_ONE, company: str = "Capital One", **extra: Any
) -> dict[str, Any]:
    return {
        "company_name": company,
        "official_career_url": url,
        "relevant_role": "Financial Analyst",
        "location": "McLean, VA",
        **extra,
    }


async def _decide(
    client: AsyncClient,
    *,
    resume_id: uuid.UUID,
    status: str,
    url: str = CAPITAL_ONE,
    company: str = "Capital One",
) -> Response:
    return await client.patch(
        DECISION,
        params={"resume_id": str(resume_id)},
        json=_page(url, company, status=status),
    )


async def _decided(
    client: AsyncClient,
    *,
    resume_id: uuid.UUID,
    status: str,
    url: str = CAPITAL_ONE,
    company: str = "Capital One",
) -> dict[str, Any]:
    """Record a decision and insist it succeeded."""
    response = await _decide(
        client, resume_id=resume_id, status=status, url=url, company=company
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def _listed(
    client: AsyncClient, *, resume_id: uuid.UUID, status: str | None = None
) -> list[dict[str, Any]]:
    params: dict[str, str] = {"resume_id": str(resume_id)}
    if status is not None:
        params["status"] = status
    response = await client.get(LIST, params=params)
    assert response.status_code == 200, response.text
    return response.json()["data"]["items"]


async def _rows(session: AsyncSession) -> int:
    return int(
        await session.scalar(
            select(func.count()).select_from(CareerPageInteraction)
        )
        or 0
    )


# --- 1, 4: a decision can be recorded ---------------------------------------


async def test_a_career_page_can_be_marked_applied(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)

    data = await _decided(api_client, resume_id=resume.id, status="applied")

    assert data["status"] == "applied"
    assert data["applied_at"] is not None
    assert data["company_name"] == "Capital One"
    assert data["kind"] == "career_page"


async def test_a_career_page_can_be_marked_skipped(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)

    data = await _decided(api_client, resume_id=resume.id, status="skipped")

    assert data["status"] == "skipped"
    # A skip is a decision, not a submission: it must not claim a time the
    # user never applied at.
    assert data["applied_at"] is None


async def test_a_page_that_cannot_be_identified_is_refused(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    """Better a 422 than a decision that silently fails to suppress anything."""
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)

    response = await api_client.patch(
        DECISION,
        params={"resume_id": str(resume.id)},
        json={
            "company_name": "   ",
            "official_career_url": "not a url",
            "status": "applied",
        },
    )

    assert response.status_code == 422, response.text


async def test_only_applied_and_skipped_are_accepted(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    """A career page has no interview, rejection or offer to record."""
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)

    response = await _decide(api_client, resume_id=resume.id, status="interview")

    assert response.status_code == 422, response.text


# --- 3, 6, 7, 8: history categorisation, and no contradiction ---------------


async def test_an_applied_page_appears_under_applied(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)
    await _decided(api_client, resume_id=resume.id, status="applied")

    applied = await _listed(api_client, resume_id=resume.id, status="applied")

    assert [item["company_name"] for item in applied] == ["Capital One"]


async def test_a_skipped_page_appears_under_skipped(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)
    await _decided(
        api_client, resume_id=resume.id, status="skipped", url=BOOZ_ALLEN,
        company="Booz Allen",
    )

    skipped = await _listed(api_client, resume_id=resume.id, status="skipped")

    assert [item["company_name"] for item in skipped] == ["Booz Allen"]


async def test_a_skipped_page_never_appears_under_applied(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)
    await _decided(api_client, resume_id=resume.id, status="skipped")

    assert await _listed(api_client, resume_id=resume.id, status="applied") == []


async def test_an_applied_page_never_appears_under_skipped(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)
    await _decided(api_client, resume_id=resume.id, status="applied")

    assert await _listed(api_client, resume_id=resume.id, status="skipped") == []


# --- 13: the two states cannot both hold ------------------------------------


async def test_changing_the_decision_replaces_it_rather_than_adding_one(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    """Applied then Skipped leaves exactly one row, saying Skipped."""
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)

    await _decided(api_client, resume_id=resume.id, status="applied")
    data = await _decided(api_client, resume_id=resume.id, status="skipped")

    assert data["status"] == "skipped"
    # applied_at cleared with the status, so no row claims a submission that
    # was taken back. The CHECK constraint enforces this too.
    assert data["applied_at"] is None
    assert await _rows(db_session) == 1
    assert await _listed(api_client, resume_id=resume.id, status="applied") == []
    assert len(await _listed(api_client, resume_id=resume.id, status="skipped")) == 1


# --- 11, 12: idempotency ----------------------------------------------------


async def test_repeating_apply_is_idempotent(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)

    first = await _decided(api_client, resume_id=resume.id, status="applied")
    response = await _decide(api_client, resume_id=resume.id, status="applied")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["data"]["id"] == first["id"], "the same row, not a second one"
    assert body["message"] == "Already recorded; nothing changed."
    # applied_at is not re-stamped by a repeat click.
    assert body["data"]["applied_at"] == first["applied_at"]
    assert await _rows(db_session) == 1


async def test_repeating_skip_is_idempotent(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)

    first = await _decided(api_client, resume_id=resume.id, status="skipped")
    response = await _decide(api_client, resume_id=resume.id, status="skipped")

    assert response.status_code == 200, response.text
    assert response.json()["data"]["id"] == first["id"]
    assert await _rows(db_session) == 1


# --- 14: identity ------------------------------------------------------------


async def test_canonically_equivalent_urls_are_one_career_page(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    """Tracking parameters, casing and www. must not fork the decision."""
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)

    await _decided(
        api_client, resume_id=resume.id, status="applied",
        url="https://www.capitalonecareers.com/?utm_source=portal&gh_src=abc",
    )
    await _decided(
        api_client, resume_id=resume.id, status="applied",
        url="http://capitalonecareers.com/",
    )

    assert await _rows(db_session) == 1, "one page, one decision"


def test_the_same_page_written_differently_hashes_alike() -> None:
    """Scheme, www., casing, tracking parameters and a legal suffix."""
    plain = career_page_key(
        official_career_url="https://www.capitalonecareers.com/",
        company_name="Capital One",
    )
    tracked = career_page_key(
        official_career_url="http://CapitalOneCareers.com/?utm_campaign=x",
        company_name="Capital One, Inc.",
    )

    assert plain is not None
    assert plain == tracked


def test_two_openings_at_one_employer_are_one_career_page() -> None:
    """The regression that live verification found.

    Some ATS URLs cannot be trimmed back to a careers root, so the career URL
    keeps a job-specific query string. Keying on the URL gave that employer a
    new identity every run, and the decision silently stopped suppressing
    anything - the page just kept coming back.
    """
    first = career_page_key(
        official_career_url=(
            "https://www.paycomonline.net/v4/ats/web.php/jobs/"
            "ViewJobDetails?job=349427&clientkey=9A1A"
        ),
        company_name="RELI GROUP INC",
    )
    later = career_page_key(
        official_career_url=(
            "https://www.paycomonline.net/v4/ats/web.php/jobs/"
            "ViewJobDetails?job=811902&clientkey=9A1A"
        ),
        company_name="Reli Group, Inc.",
    )

    assert first is not None
    assert first == later


def test_two_employers_sharing_an_ats_host_stay_distinct() -> None:
    """Which is why the host cannot be the identity on its own."""
    host = "https://www.paycomonline.net/v4/ats/web.php/jobs/ViewJobDetails?job=1"

    assert career_page_key(
        official_career_url=host, company_name="RELI GROUP INC"
    ) != career_page_key(official_career_url=host, company_name="Acme Widgets")


def test_a_company_with_no_usable_name_has_no_identity() -> None:
    """A bare host is shared by every employer on that ATS."""
    assert career_page_key(
        official_career_url="https://www.paycomonline.net/v4/ats/web.php",
        company_name=None,
    ) is None


def test_two_different_employers_do_not_share_an_identity() -> None:
    assert career_page_key(
        official_career_url=CAPITAL_ONE, company_name="Capital One"
    ) != career_page_key(
        official_career_url=BOOZ_ALLEN, company_name="Booz Allen"
    )


def test_a_page_with_nothing_usable_has_no_identity() -> None:
    assert career_page_key(official_career_url=None, company_name=None) is None


# --- 9, 10: resume scoping ---------------------------------------------------


async def test_a_decision_belongs_to_one_resume(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await _acting_user(db_session, settings)
    resume_a = await make_resume(db_session, user=user)
    resume_b = await make_resume(db_session, user=user)

    await _decided(api_client, resume_id=resume_a.id, status="applied")

    assert len(await _listed(api_client, resume_id=resume_a.id)) == 1
    assert await _listed(api_client, resume_id=resume_b.id) == []


async def test_one_resume_applying_does_not_suppress_the_page_for_another(
    db_session: AsyncSession, settings: Settings, api_client: AsyncClient
) -> None:
    """The rule in CLAUDE.md section 11, at the level the search reads."""
    user = await _acting_user(db_session, settings)
    resume_a = await make_resume(db_session, user=user)
    resume_b = await make_resume(db_session, user=user)

    await _decided(api_client, resume_id=resume_a.id, status="applied")

    suppressed_a = await career_page_service.ineligible_career_page_keys(
        db_session, resume_id=resume_a.id
    )
    suppressed_b = await career_page_service.ineligible_career_page_keys(
        db_session, resume_id=resume_b.id
    )

    assert len(suppressed_a) == 1
    assert suppressed_b == frozenset(), "Resume B never decided anything"


# --- 2, 5: the suppression the search pipeline reads -------------------------


async def test_both_decisions_suppress_the_page_for_that_resume(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    """Applied and Skipped both remove a page from future runs."""
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)

    await _decided(api_client, resume_id=resume.id, status="applied")
    await _decided(
        api_client, resume_id=resume.id, status="skipped", url=BOOZ_ALLEN,
        company="Booz Allen",
    )

    suppressed = await career_page_service.ineligible_career_page_keys(
        db_session, resume_id=resume.id
    )

    assert career_page_key(
        official_career_url=CAPITAL_ONE, company_name="Capital One"
    ) in suppressed
    assert career_page_key(
        official_career_url=BOOZ_ALLEN, company_name="Booz Allen"
    ) in suppressed
    # The page never acted on stays available.
    assert career_page_key(
        official_career_url=COGNIZANT, company_name="Cognizant"
    ) not in suppressed


async def test_an_undecided_page_is_not_suppressed(
    db_session: AsyncSession, settings: Settings
) -> None:
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)

    assert await career_page_service.ineligible_career_page_keys(
        db_session, resume_id=resume.id
    ) == frozenset()


async def test_undoing_a_decision_makes_the_page_available_again(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)
    await _decided(api_client, resume_id=resume.id, status="applied")

    response = await api_client.delete(
        DECISION,
        params={
            "resume_id": str(resume.id),
            "official_career_url": CAPITAL_ONE,
            "company_name": "Capital One",
        },
    )

    assert response.status_code == 200, response.text
    assert await career_page_service.ineligible_career_page_keys(
        db_session, resume_id=resume.id
    ) == frozenset()


# --- Reading one page's state ------------------------------------------------


async def test_state_reports_no_decision_rather_than_erroring(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)

    response = await api_client.get(
        STATE,
        params={
            "resume_id": str(resume.id),
            "official_career_url": CAPITAL_ONE,
            "company_name": "Capital One",
        },
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["interaction"] is None
    assert data["status"] is None
    assert data["career_page_key"]


async def test_state_reports_the_recorded_decision(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)
    await _decided(api_client, resume_id=resume.id, status="skipped")

    response = await api_client.get(
        STATE,
        params={
            "resume_id": str(resume.id),
            "official_career_url": CAPITAL_ONE,
            "company_name": "Capital One",
        },
    )

    assert response.json()["data"]["status"] == "skipped"


# --- 17, 18: the boundary that must not be crossed ---------------------------


async def test_a_career_page_decision_creates_no_job_application(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    """Applying to a career page is not applying to the jobs on it."""
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)
    before = int(
        await db_session.scalar(select(func.count()).select_from(Application)) or 0
    )

    await _decided(api_client, resume_id=resume.id, status="applied")

    after = int(
        await db_session.scalar(select(func.count()).select_from(Application)) or 0
    )
    assert after == before, "no fake job application was written"


async def test_a_career_page_decision_leaves_that_companys_jobs_alone(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    """Skipping an employer's page does not skip their individual vacancies."""
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)
    job = await make_job(db_session, company="Capital One")

    await _decided(api_client, resume_id=resume.id, status="skipped")

    application = await db_session.scalar(
        select(Application).where(
            Application.resume_id == resume.id, Application.job_id == job.id
        )
    )
    assert application is None, "the company's job was not touched"

    # And the job lane's own exclusion set is unaffected.
    from app.matching.eligibility import ineligible_job_ids

    assert await ineligible_job_ids(db_session, resume_id=resume.id) == frozenset()


# --- 15, 16: the job contract is unchanged -----------------------------------


async def test_job_apply_and_skip_still_behave_as_before(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    """The job lane keeps its own semantics, including its history rows."""
    from app.models.application import ApplicationStatusHistory

    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)
    job = await make_job(db_session)

    response = await api_client.patch(
        f"/api/v1/applications/{job.id}",
        params={"resume_id": str(resume.id)},
        json={"status": "applied"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "applied"
    assert response.json()["data"]["applied_at"] is not None

    # A job application still writes status history; career pages have none to
    # write, and neither borrowed the other's behaviour.
    history = int(
        await db_session.scalar(
            select(func.count()).select_from(ApplicationStatusHistory)
        )
        or 0
    )
    assert history > 0


async def test_a_job_application_is_not_listed_as_a_career_page(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)
    job = await make_job(db_session)

    await api_client.patch(
        f"/api/v1/applications/{job.id}",
        params={"resume_id": str(resume.id)},
        json={"status": "applied"},
    )

    assert await _listed(api_client, resume_id=resume.id) == []


async def test_a_career_page_decision_is_not_listed_as_a_job_application(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)

    await _decided(api_client, resume_id=resume.id, status="applied")

    response = await api_client.get(
        "/api/v1/applications", params={"resume_id": str(resume.id)}
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["items"] == []


# --- Storage shape -----------------------------------------------------------


async def test_the_decision_snapshots_what_the_user_acted_on(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    """Career pages are not stored, so history reads the snapshot."""
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)

    await _decided(api_client, resume_id=resume.id, status="applied")

    row = await db_session.scalar(select(CareerPageInteraction))
    assert row is not None
    assert row.company_name == "Capital One"
    assert row.official_career_url == CAPITAL_ONE
    assert row.relevant_role == "Financial Analyst"
    assert row.location == "McLean, VA"
    assert row.status is ApplicationStatus.APPLIED
