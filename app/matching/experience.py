"""Experience and seniority compatibility.

Two rules do most of the work here, and both are about not inventing things:

* **A posting that states no experience requirement is not a rejection.**
  Entry-level and mid-level adverts frequently give no number at all, and
  treating silence as "zero years required" or "ten years required" would be
  a guess either way.
* **A resume that states no years of experience is not zero years.** The
  analysis layer leaves it None when the resume gives nothing verifiable, and
  scoring keeps that as unknown.

Only a clear, stated gap rejects. Everything else costs points and says why.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.constants import SENIORITY_RANK, Seniority
from app.matching.profile import ResumeMatchProfile
from app.matching.requirements import JobRequirements

#: A shortfall this large against a stated minimum is not a stretch, it is the
#: wrong candidate. Below it the job is rejected outright.
REJECT_YEARS_SHORTFALL = 3.0

#: Within this shortfall the gap is a stretch worth showing, lightly penalised.
TOLERATED_YEARS_SHORTFALL = 1.0

#: Being this far above a stated maximum costs a little: the posting is likely
#: pitched below the candidate. It never rejects - applying down is the
#: candidate's decision to make, not the system's.
OVERQUALIFIED_MARGIN = 5.0


@dataclass(frozen=True, slots=True)
class ExperienceAssessment:
    """Years-of-experience and seniority fit, as a 0.0-1.0 factor."""

    factor: float
    years_factor: float
    seniority_factor: float
    summary: str
    penalties: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    #: Set when the shortfall is large enough to reject.
    insufficient_experience: bool = False
    shortfall_years: float | None = None


def evaluate_experience(
    profile: ResumeMatchProfile, job: JobRequirements
) -> ExperienceAssessment:
    """Score how well the candidate's experience fits the posting."""
    years_factor, years_penalties, years_evidence, shortfall = _years_factor(
        profile, job
    )
    seniority_factor, seniority_penalties, seniority_evidence = _seniority_factor(
        profile, job
    )

    # Years carry more than the title's seniority word: a stated requirement is
    # firmer evidence than a label an employer chose for a job advert.
    factor = round(0.6 * years_factor + 0.4 * seniority_factor, 6)

    insufficient = shortfall is not None and shortfall > REJECT_YEARS_SHORTFALL

    return ExperienceAssessment(
        factor=factor,
        years_factor=years_factor,
        seniority_factor=seniority_factor,
        summary=_summarise(profile, job, shortfall),
        penalties=(*years_penalties, *seniority_penalties),
        evidence=(*years_evidence, *seniority_evidence),
        insufficient_experience=insufficient,
        shortfall_years=shortfall,
    )


def _years_factor(
    profile: ResumeMatchProfile, job: JobRequirements
) -> tuple[float, tuple[str, ...], tuple[str, ...], float | None]:
    if job.min_years is None:
        # Nothing stated. Neither a pass nor a fail; full credit, because
        # there is no requirement to fall short of.
        return 1.0, (), ("The posting states no experience requirement.",), None

    if profile.years_experience is None:
        # The requirement is stated but the resume does not evidence years.
        # A modest penalty, never a rejection: the resume may simply not spell
        # it out.
        return (
            0.7,
            ("The resume does not state total years of experience.",),
            (f"The posting asks for {job.min_years:g}+ years.",),
            None,
        )

    candidate = profile.years_experience
    shortfall = job.min_years - candidate

    if shortfall <= 0:
        evidence = [
            f"{candidate:g} years of experience meets the "
            f"{job.min_years:g}-year requirement."
        ]
        if (
            job.max_years is not None
            and candidate > job.max_years + OVERQUALIFIED_MARGIN
        ):
            return (
                0.85,
                (
                    f"{candidate:g} years is well above the posting's "
                    f"{job.max_years:g}-year ceiling.",
                ),
                tuple(evidence),
                None,
            )
        return 1.0, (), tuple(evidence), None

    if shortfall <= TOLERATED_YEARS_SHORTFALL:
        return (
            0.8,
            (
                f"{candidate:g} years is {shortfall:g} short of the "
                f"{job.min_years:g} years asked for.",
            ),
            (job.years_evidence or "",),
            shortfall,
        )

    return (
        0.35,
        (
            f"{candidate:g} years is {shortfall:g} short of the "
            f"{job.min_years:g} years asked for.",
        ),
        (job.years_evidence or "",),
        shortfall,
    )


def _seniority_factor(
    profile: ResumeMatchProfile, job: JobRequirements
) -> tuple[float, tuple[str, ...], tuple[str, ...]]:
    candidate = profile.seniority_rank
    posting = job.title_seniority_rank

    if candidate is None or posting is None:
        return 1.0, (), ("Seniority not stated on both sides; not penalised.",)

    gap = posting - candidate
    candidate_label = profile.seniority.value if profile.seniority else "unknown"
    posting_label = job.title_seniority.value if job.title_seniority else "unknown"

    if gap == 0:
        return (
            1.0,
            (),
            (f"Seniority matches ({candidate_label}).",),
        )

    if abs(gap) == 1:
        return (
            0.85,
            (
                f"Posting is {posting_label} against a {candidate_label} "
                "resume, one level apart.",
            ),
            (),
        )

    if gap > 0:
        return (
            0.4,
            (
                f"Posting is {posting_label}, {gap} levels above the resume's "
                f"{candidate_label}.",
            ),
            (),
        )

    # The candidate is more senior than the posting. Worth less of a penalty:
    # applying to a more junior role is a legitimate choice.
    return (
        0.7,
        (
            f"Posting is {posting_label}, {abs(gap)} levels below the resume's "
            f"{candidate_label}.",
        ),
        (),
    )


def _summarise(
    profile: ResumeMatchProfile, job: JobRequirements, shortfall: float | None
) -> str:
    if job.min_years is None:
        return "No experience requirement stated."
    if profile.years_experience is None:
        return f"Posting asks for {job.min_years:g}+ years; resume does not state a total."
    if shortfall is None or shortfall <= 0:
        return (
            f"{profile.years_experience:g} years against a "
            f"{job.min_years:g}-year requirement."
        )
    return (
        f"{profile.years_experience:g} years against a {job.min_years:g}-year "
        f"requirement, {shortfall:g} short."
    )
