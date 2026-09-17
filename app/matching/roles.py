"""The role/title gate: is this even the right job?

This runs **before** any skill scoring, and its rejections are final. A high
generic skill overlap must never override an incompatible role (CLAUDE.md
section 3, Phase 6 brief): a Financial Analyst and an Accountant share Excel,
SQL and financial statements, and that is exactly why skill overlap cannot be
allowed a vote here.

The checks run in this order, and the first one to decide wins:

1. **Leadership** - a posting whose title carries manager, director,
   principal, lead or executive responsibility the candidate is not near.
2. **Exact role** - the seniority-stripped title resolves to a taxonomy role.
   The target role passes; any other role is rejected outright, whatever else
   the posting says.
3. **Approved variation** - the title matches one of the resume's own
   same-role search titles.
4. **Foreign role phrase** - the title contains another profession's name.
   Checked before the fuzzy tiers so nothing can slip through them.
5. **Strong variant** - the title contains the role title as a phrase, e.g.
   "Financial Analyst, Service Lines".
6. **Weak variant** - every word of the role title appears, and the head noun
   matches, e.g. "Financial Revenue Analyst".
7. Anything else is rejected.

# Comma-inverted titles

One family of titles fails all seven checks for a reason that has nothing to do
with the job: some employers write the head noun first and qualify it after a
comma. "Analyst, FP&A" is "FP&A Analyst"; "Engineer, Machine Learning" is a
Machine Learning Engineer. The checks above compare token *sequences*, so an
inverted title can never match a phrase, and the posting is refused as the
wrong role.

So a title that reaches step 7 and carries a single comma is tried once more in
its de-inverted form, through these very same checks. Two things keep that from
becoming a loophole:

* the rescue is only reached from step 7. A title already judged a different
  profession (step 2 or 4) or too senior (step 1) has returned long before, so
  no foreign-role or leadership rejection can be undone by it;
* only EXACT and STRONG_VARIANT are accepted back. The weak tier is where the
  noise lives - "Lead Analyst, Financial Crimes" de-inverts to a weak variant of
  Financial Analyst, and an AML job is not an FP&A job - so a weak answer leaves
  the original rejection standing.

Measured over the recall audit's captured postings: 19 listings reached the
rescue, all of them already fetched, and the tier restriction admitted the ones
that were genuinely the same job while leaving the weak tier refused.

# Slash-joined titles

The same thing happens for a different reason with "AI/ML Engineer". Here the
title is not reordered, it is *abbreviated*: the employer has named two
interchangeable ways of saying the job and shared one head noun between them.
``normalize_token`` keeps "/" on purpose - it is what makes CI/CD, TCP/IP and
LAN/WAN single skills rather than pairs - so "ai/ml" stays one token and can
never match "ml engineer".

So a slashed title refused at step 7 is read once as each side, splitting only
the slashed word and leaving the shared head in place. The tier restriction
above applies unchanged, and one further rule applies only here: if *any*
reading names a different profession, the whole rescue is abandoned. Two
readings are two claims about what the job is, and "Financial
Analyst/Accountant" advertises two careers at once - taking the half that
flatters the resume is precisely the collapse this gate exists to prevent.

The rewrites are tried comma first, then slash, so a title carrying both has
one predictable answer. Neither ever calls ``evaluate_role`` again, so no
rewrite can be applied to the output of another.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import StrEnum

from app.analysis import taxonomy
from app.analysis.text import normalize_title
from app.core.constants import SENIORITY_RANK, Seniority
from app.matching.profile import ResumeMatchProfile
from app.matching.requirements import JobRequirements, strip_seniority

#: Titles at these levels carry responsibility a candidate has to be near.
LEADERSHIP_LEVELS: frozenset[Seniority] = frozenset(
    {
        Seniority.LEAD,
        Seniority.PRINCIPAL,
        Seniority.MANAGER,
        Seniority.DIRECTOR,
        Seniority.EXECUTIVE,
    }
)

#: How far below a leadership title a candidate may sit before it is rejected.
#: One rank is a stretch worth showing; two is a different job.
MAX_LEADERSHIP_GAP = 1


class RoleVerdict(StrEnum):
    """How the job's title relates to the resume's target role."""

    EXACT = "exact"
    VARIATION = "variation"
    STRONG_VARIANT = "strong_variant"
    WEAK_VARIANT = "weak_variant"
    INCOMPATIBLE = "incompatible"


#: Fraction of the role component each verdict earns. A weak variant is still
#: the right family, just less certainly the same job.
ROLE_VERDICT_FACTOR: dict[RoleVerdict, float] = {
    RoleVerdict.EXACT: 1.0,
    RoleVerdict.VARIATION: 1.0,
    RoleVerdict.STRONG_VARIANT: 0.9,
    RoleVerdict.WEAK_VARIANT: 0.75,
    RoleVerdict.INCOMPATIBLE: 0.0,
}


class RoleRejection(StrEnum):
    """Why a posting failed the gate. Stored on the match record."""

    NO_TARGET_ROLE = "no_target_role"
    ROLE_FAMILY_MISMATCH = "role_family_mismatch"
    ROLE_INCOMPATIBLE = "role_incompatible"
    LEADERSHIP_MISMATCH = "leadership_responsibility_mismatch"


@dataclass(frozen=True, slots=True)
class RoleAssessment:
    """The gate's decision, with the evidence that produced it."""

    verdict: RoleVerdict
    factor: float
    reason: str
    detected_role_key: str | None = None
    rejection: RoleRejection | None = None

    @property
    def passed(self) -> bool:
        return self.verdict is not RoleVerdict.INCOMPATIBLE


#: Verdicts the comma rescue may return. STRONG_VARIANT is the loosest tier
#: that still requires the role's own title to appear as a whole phrase; the
#: weak tier only requires the same words in any arrangement, which is how an
#: unrelated speciality gets in.
_RESCUABLE_VERDICTS: frozenset[RoleVerdict] = frozenset(
    {RoleVerdict.EXACT, RoleVerdict.STRONG_VARIANT}
)

#: Employers write the separator both ways: "AI/ML Engineer" and
#: "Network Engineer / Architect". Collapsing the spaces first means one rule
#: handles both.
_SLASH_SPACING_RE = re.compile(r"\s*/\s*")


def evaluate_role(
    profile: ResumeMatchProfile, job: JobRequirements
) -> RoleAssessment:
    """Decide whether this posting is the candidate's role at all.

    Runs the checks once as written, then - only for a title refused at the
    last step for not being the target role - once more on its de-inverted
    form. See "Comma-inverted titles" above.
    """
    assessment = _assess(profile, job)

    if assessment.rejection is not RoleRejection.ROLE_INCOMPATIBLE:
        # Passed, or refused by a rule the rescue is not allowed to revisit:
        # leadership, a named foreign profession, or a missing target role.
        return assessment

    # The two rewrites are tried in a fixed order - comma, then slash - so a
    # title carrying both punctuation marks has one predictable answer. Each is
    # re-run through the same checks. `title_seniority` is deliberately carried
    # over untouched, so the leadership rule still judges the seniority the
    # employer actually wrote.
    candidate = _comma_inverted_title(job.title)
    if candidate is not None:
        rescued = _assess(
            profile,
            replace(job, title=candidate, role_phrase=strip_seniority(candidate)),
        )
        if rescued.verdict in _RESCUABLE_VERDICTS:
            return replace(
                rescued,
                reason=(
                    f"Title {job.title!r} is {profile.target_role_display} "
                    f"written head-noun first; read as {candidate!r}, "
                    f"{rescued.reason[0].lower()}{rescued.reason[1:]}"
                ),
            )

    return _slash_rescue(profile, job) or assessment


def _slash_rescue(
    profile: ResumeMatchProfile, job: JobRequirements
) -> RoleAssessment | None:
    """Read a slash-joined title as each of the roles it names, or give up.

    Unlike the comma rewrite, which re-reads one title one way, this proposes
    *competing* readings: "AI/ML Engineer" is both an AI Engineer and an ML
    Engineer, and the posting means one job that answers to either. That
    difference is why this path carries a veto the comma path does not need.

    Every candidate is put through the whole gate, and if **any** of them names
    a different profession the rescue is abandoned outright - even when another
    candidate would have passed. "Financial Analyst/Accountant" is a posting
    advertising two careers at once, and picking the flattering half of it is
    exactly the collapse of role families the gate exists to prevent.
    """
    candidates = _slash_title_candidates(job.title)
    if not candidates:
        return None

    best: RoleAssessment | None = None
    best_title = ""

    for candidate in candidates:
        assessed = _assess(
            profile,
            replace(job, title=candidate, role_phrase=strip_seniority(candidate)),
        )
        if assessed.rejection is RoleRejection.ROLE_FAMILY_MISMATCH:
            # One reading of this title is a different career. Refuse all of it.
            return None
        if assessed.verdict not in _RESCUABLE_VERDICTS:
            continue
        if best is None or assessed.factor > best.factor:
            best, best_title = assessed, candidate

    if best is None:
        return None

    return replace(
        best,
        reason=(
            f"Title {job.title!r} names {profile.target_role_display} on one "
            f"side of the slash; read as {best_title!r}, "
            f"{best.reason[0].lower()}{best.reason[1:]}"
        ),
    )


def _slash_title_candidates(title: str) -> tuple[str, ...]:
    """Split "AI/ML Engineer" into "AI Engineer" and "ML Engineer".

    Only the slashed word is split, and the rest of the title - crucially the
    shared head noun - is left in place. Replacing every "/" with a space would
    turn "AI/ML Engineer" into the three-word "AI ML Engineer", which is not a
    title anyone wrote and which the gate would have to guess at.

    Deliberately narrow, for the same reason the comma rewrite is:

    * exactly one slash in the whole title, and in exactly one word, so
      "Data/ML/AI Engineer" and "Analyst - Risk/EMEA, Global" are left alone;
    * both sides non-empty and alphabetic, so "24/7 Support Engineer" and
      "Engineer (1/2 time)" produce nothing;
    * at least one other word, so a bare "Analyst/Consultant" with no shared
      head is not rewritten into two unrelated one-word titles.

    This operates on the job title only. ``normalize_token`` still keeps "/"
    for everything else, so CI/CD, TCP/IP and LAN/WAN remain single skills.
    """
    spaced = _SLASH_SPACING_RE.sub("/", title.strip())
    if spaced.count("/") != 1:
        return ()

    words = spaced.split()
    positions = [index for index, word in enumerate(words) if "/" in word]
    if len(positions) != 1 or len(words) < 2:
        return ()

    index = positions[0]
    left, _, right = words[index].partition("/")
    if not left or not right:
        return ()
    if not left.isalpha() or not right.isalpha():
        return ()

    return tuple(
        " ".join(words[:index] + [side] + words[index + 1 :])
        for side in (left, right)
    )


def _comma_inverted_title(title: str) -> str | None:
    """Rewrite "Analyst, FP&A" as "FP&A Analyst", or return None.

    Deliberately narrow. Arbitrary comma-separated prose is not a title, so a
    rewrite is only offered when the punctuation looks like this convention and
    nothing else:

    * exactly one comma - "Analyst, Risk, EMEA" is a list, not an inversion;
    * both sides non-empty, so a trailing comma produces nothing;
    * a short head (<=3 words, e.g. "Senior Analyst II") and a short tail
      (<=4 words, e.g. "Customer Insights & Analytics").

    The bounds matter: without them a long clause after the comma would be
    swung in front of the head noun and could contain almost anything.
    """
    if title.count(",") != 1:
        return None

    head, _, tail = title.partition(",")
    head, tail = head.strip(), tail.strip()
    if not head or not tail:
        return None
    if len(head.split()) > 3 or len(tail.split()) > 4:
        return None

    return f"{tail} {head}"


def _assess(profile: ResumeMatchProfile, job: JobRequirements) -> RoleAssessment:
    """The seven checks, in order. Called twice at most, never recursively."""
    if not profile.is_scoreable:
        return RoleAssessment(
            verdict=RoleVerdict.INCOMPATIBLE,
            factor=0.0,
            reason=(
                "The resume has no detected target role, so no job can be "
                "matched against it."
            ),
            rejection=RoleRejection.NO_TARGET_ROLE,
        )

    role_spec = profile.role_spec
    role_phrase = job.role_phrase
    detected = taxonomy.role_key_for_term(role_phrase)

    # A title that names another profession is reported as that, before the
    # leadership check: "Staff Accountant" is rejected for being an accountant,
    # not for its seniority, and the reason the user reads should say so.
    if detected is not None and detected != profile.target_role_key:
        other = taxonomy.get_role(detected)
        return RoleAssessment(
            verdict=RoleVerdict.INCOMPATIBLE,
            factor=0.0,
            reason=(
                f"Title is {other.canonical_title if other else detected}, a "
                f"different role from {profile.target_role_display}. Shared "
                "skills do not make it the same job."
            ),
            detected_role_key=detected,
            rejection=RoleRejection.ROLE_FAMILY_MISMATCH,
        )

    leadership = _leadership_rejection(profile, job)
    if leadership is not None:
        return leadership

    if detected is not None:
        return RoleAssessment(
            verdict=RoleVerdict.EXACT,
            factor=ROLE_VERDICT_FACTOR[RoleVerdict.EXACT],
            reason=f"Title is the target role ({profile.target_role_display}).",
            detected_role_key=detected,
        )

    # 3. One of the resume's approved same-role search titles.
    if role_phrase in profile.role_variation_keys:
        return RoleAssessment(
            verdict=RoleVerdict.VARIATION,
            factor=ROLE_VERDICT_FACTOR[RoleVerdict.VARIATION],
            reason=f"Title is an approved variation of {profile.target_role_display}.",
            detected_role_key=profile.target_role_key,
        )

    # 4. Another profession named inside a longer title.
    foreign = _foreign_role_phrase(role_phrase, profile.target_role_key)
    if foreign is not None:
        other = taxonomy.get_role(foreign)
        return RoleAssessment(
            verdict=RoleVerdict.INCOMPATIBLE,
            factor=0.0,
            reason=(
                f"Title names {other.canonical_title if other else foreign}, a "
                f"different role from {profile.target_role_display}."
            ),
            detected_role_key=foreign,
            rejection=RoleRejection.ROLE_FAMILY_MISMATCH,
        )

    if role_spec is None:  # pragma: no cover - is_scoreable implies a spec
        return RoleAssessment(
            verdict=RoleVerdict.INCOMPATIBLE,
            factor=0.0,
            reason="The resume's target role is not in the taxonomy.",
            rejection=RoleRejection.NO_TARGET_ROLE,
        )

    # 5. The role title appears as a phrase inside a longer title.
    for phrase in _target_phrases(role_spec, profile):
        if _contains_phrase(role_phrase, phrase):
            return RoleAssessment(
                verdict=RoleVerdict.STRONG_VARIANT,
                factor=ROLE_VERDICT_FACTOR[RoleVerdict.STRONG_VARIANT],
                reason=(
                    f"Title contains {phrase!r}, a qualified form of "
                    f"{profile.target_role_display}."
                ),
                detected_role_key=profile.target_role_key,
            )

    # 6. Every word of the role title is present and the head noun matches.
    canonical_tokens = set(normalize_title(role_spec.canonical_title).split())
    job_tokens = role_phrase.split()
    head_matches = bool(job_tokens) and job_tokens[-1] == _head_noun(role_spec)

    if head_matches and canonical_tokens <= set(job_tokens):
        return RoleAssessment(
            verdict=RoleVerdict.WEAK_VARIANT,
            factor=ROLE_VERDICT_FACTOR[RoleVerdict.WEAK_VARIANT],
            reason=(
                f"Title is a qualified {profile.target_role_display} "
                "with an extra specialisation."
            ),
            detected_role_key=profile.target_role_key,
        )

    return RoleAssessment(
        verdict=RoleVerdict.INCOMPATIBLE,
        factor=0.0,
        reason=(
            f"Title {job.title!r} is not {profile.target_role_display} or an "
            "approved variation of it."
        ),
        rejection=RoleRejection.ROLE_INCOMPATIBLE,
    )


def _leadership_rejection(
    profile: ResumeMatchProfile, job: JobRequirements
) -> RoleAssessment | None:
    """Reject a leadership posting the candidate is not close to.

    Only rejects when the resume states a seniority. Without one there is no
    basis for the comparison, and inventing one would reject jobs on a guess.
    """
    job_level = job.title_seniority
    if job_level is None or job_level not in LEADERSHIP_LEVELS:
        return None

    candidate_rank = profile.seniority_rank
    if candidate_rank is None:
        return None

    gap = SENIORITY_RANK[job_level] - candidate_rank
    if gap <= MAX_LEADERSHIP_GAP:
        return None

    return RoleAssessment(
        verdict=RoleVerdict.INCOMPATIBLE,
        factor=0.0,
        reason=(
            f"Title carries {job_level.value} responsibility, "
            f"{gap} levels above the resume's {profile.seniority.value if profile.seniority else 'unknown'} level."
        ),
        rejection=RoleRejection.LEADERSHIP_MISMATCH,
    )


def _target_phrases(
    role_spec: taxonomy.RoleSpec, profile: ResumeMatchProfile
) -> tuple[str, ...]:
    """Phrases whose presence identifies the target role, longest first."""
    phrases = {
        normalize_title(role_spec.canonical_title),
        *(normalize_title(v) for v in role_spec.variations),
        *role_spec.aliases,
        *profile.role_variation_keys,
    }

    return tuple(sorted((p for p in phrases if p), key=len, reverse=True))


def _foreign_role_phrase(role_phrase: str, target_key: str | None) -> str | None:
    """The key of another role named inside this title, if any.

    Only whole-phrase matches count, so "Business Financial Analyst" does not
    register "business analyst" - those words are not adjacent.
    """
    for key, role in taxonomy.ROLES.items():
        if key == target_key:
            continue

        for term in (normalize_title(role.canonical_title), *role.aliases):
            if term and _contains_phrase(role_phrase, term):
                return key

    return None


def _contains_phrase(haystack: str, needle: str) -> bool:
    """Whole-token phrase containment, so "analyst" never matches "analysts"."""
    if not needle:
        return False

    haystack_tokens = haystack.split()
    needle_tokens = needle.split()
    if len(needle_tokens) > len(haystack_tokens):
        return False

    for start in range(len(haystack_tokens) - len(needle_tokens) + 1):
        if haystack_tokens[start : start + len(needle_tokens)] == needle_tokens:
            return True

    return False


def _head_noun(role_spec: taxonomy.RoleSpec) -> str:
    tokens = normalize_title(role_spec.canonical_title).split()
    return tokens[-1] if tokens else ""
