"""Domain and target-role detection.

CLAUDE.md section 3 requires exactly ONE primary domain and ONE primary target
role. Scoring therefore produces a single winner; runners-up are retained only
as debugging data on the ``Detection`` and are never presented to the user as
alternative careers.

Signals, strongest first:
  1. the most recent job title
  2. other job titles in the experience section
  3. a title stated in the summary
  4. overlap with the role's core skills

When two roles score EXACTLY the same, the evidence has not chosen and
something else must. Preferring the tied role that belongs to the resume's own
domain is the only additional evidence available, and it is used for that case
alone: it never overrides a higher-scoring role, because a role beaten on
evidence has been beaten. Where even that does not separate them the order
stays alphabetical so the result is reproducible, and the tie is recorded on
the ``Detection`` either way so the caller can say so rather than present a
coin-flip as a finding.
"""

from __future__ import annotations

from collections import defaultdict

from app.analysis import taxonomy
from app.analysis.results import Detection, Evidence, ExtractedSkill, ExtractedTitle, Section
from app.analysis.skills import skill_keys
from app.analysis.text import snippet
from app.core.constants import ResumeSection

# Signal weights.
W_MOST_RECENT_TITLE = 5.0
W_EXPERIENCE_TITLE = 2.0
W_SUMMARY_TITLE = 1.5
W_CORE_SKILL_COVERAGE = 2.0
W_DOMAIN_FROM_ROLE = 4.0
W_DOMAIN_KEYWORD = 0.4
W_DOMAIN_SKILL = 0.25

# Score at which a detection is considered fully supported.
_FULL_STRENGTH = 6.0
_MAX_KEYWORD_HITS = 6

#: Scores closer than this count as exactly equal. Contributions are sums of
#: floats, so two genuinely identical scores can differ in the last bit.
_TIE_EPSILON = 1e-9


def _confidence(winner: float, runner_up: float) -> float:
    """Blend absolute evidence strength with separation from the runner-up.

    A high score that barely beats another role is not confident, and a clear
    win on thin evidence is not confident either. Both must hold.
    """
    if winner <= 0:
        return 0.0
    strength = min(1.0, winner / _FULL_STRENGTH)
    margin = (winner - runner_up) / winner
    return round(min(1.0, strength * (0.6 + 0.4 * margin)), 4)


def detect_target_role(
    titles: list[ExtractedTitle],
    skills: list[ExtractedSkill],
    *,
    domain_hint: str | None = None,
) -> Detection:
    """Pick the single target role.

    ``domain_hint`` is the domain the resume's own keywords and skills point
    to, computed without reference to any role (see ``detect_domain`` called
    with an empty role). It breaks exact ties and nothing else. It is a hint
    rather than a signal on purpose: measured across the stored resumes, a
    correctly detected role legitimately disagrees with the keyword domain
    about a third of the time - a Business Analyst resume reads as "data" -
    so letting it do more than break ties would reject correct answers.
    """
    scores: dict[str, float] = defaultdict(float)
    evidence_by_role: dict[str, list[Evidence]] = defaultdict(list)

    for title in titles:
        if title.role_key is None:
            continue

        if title.is_most_recent:
            weight, kind = W_MOST_RECENT_TITLE, "most_recent_title"
        elif title.section is ResumeSection.EXPERIENCE:
            weight, kind = W_EXPERIENCE_TITLE, "experience_title"
        else:
            weight, kind = W_SUMMARY_TITLE, "summary_title"

        scores[title.role_key] += weight
        evidence_by_role[title.role_key].append(
            Evidence(
                kind=kind,
                value=title.normalized,
                section=title.section,
                line_number=title.line_number,
                snippet=title.raw[:160],
                weight=weight,
            )
        )

    # Core-skill overlap corroborates a title but cannot by itself invent a
    # role the resume never names.
    present = skill_keys(skills)
    for role in taxonomy.ROLES.values():
        if not role.core_skills:
            continue
        matched = present.intersection(role.core_skills)
        if not matched:
            continue

        coverage = len(matched) / len(role.core_skills)
        contribution = coverage * W_CORE_SKILL_COVERAGE
        scores[role.key] += contribution
        evidence_by_role[role.key].append(
            Evidence(
                kind="core_skill_coverage",
                value=", ".join(sorted(matched)[:6]),
                section=ResumeSection.SKILLS,
                snippet=(
                    f"{len(matched)}/{len(role.core_skills)} core skills for "
                    f"{role.canonical_title}."
                ),
                weight=round(contribution, 4),
            )
        )

    if not scores:
        return Detection(key=None, display=None, confidence=0.0)

    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    top_score = ranked[0][1]
    # Already alphabetical within a score, so every list built from this stays
    # deterministic whichever branch below is taken.
    tied = [key for key, score in ranked if abs(score - top_score) <= _TIE_EPSILON]

    winner_key = tied[0]
    tie_break: str | None = None
    if len(tied) > 1:
        tie_break = "alphabetical"
        if domain_hint is not None:
            agreeing = [
                key for key in tied if taxonomy.ROLES[key].domain_key == domain_hint
            ]
            if agreeing:
                winner_key = agreeing[0]
                tie_break = "domain_agreement"

    winner_score = scores[winner_key]
    runner_up_score = next(
        (score for key, score in ranked if key != winner_key), 0.0
    )

    role = taxonomy.ROLES[winner_key]
    return Detection(
        key=winner_key,
        display=role.canonical_title,
        confidence=_confidence(winner_score, runner_up_score),
        evidence=sorted(
            evidence_by_role[winner_key], key=lambda e: -e.weight
        )[:6],
        alternatives=[
            (key, round(score, 4)) for key, score in ranked if key != winner_key
        ][:3],
        tied_with=[key for key in tied if key != winner_key],
        tie_break=tie_break,
    )


def detect_domain(
    role_detection: Detection,
    skills: list[ExtractedSkill],
    sections: list[Section],
    full_text: str,
) -> Detection:
    """Pick the single primary domain.

    The detected role is the dominant signal, since a role belongs to exactly
    one domain. Keywords and skills corroborate, and carry the decision alone
    when no role was found.
    """
    scores: dict[str, float] = defaultdict(float)
    evidence_by_domain: dict[str, list[Evidence]] = defaultdict(list)

    if role_detection.key is not None:
        role = taxonomy.ROLES[role_detection.key]
        # Scaled by role confidence so an uncertain role cannot force a domain.
        contribution = W_DOMAIN_FROM_ROLE * max(role_detection.confidence, 0.25)
        scores[role.domain_key] += contribution
        evidence_by_domain[role.domain_key].append(
            Evidence(
                kind="domain_from_role",
                value=role.canonical_title,
                snippet=(
                    f"Detected role {role.canonical_title} belongs to "
                    f"{taxonomy.DOMAINS[role.domain_key].display_name}."
                ),
                weight=round(contribution, 4),
            )
        )

    # Domain keyword phrases.
    keyword_hits: dict[str, int] = defaultdict(int)
    pattern = taxonomy.domain_keyword_pattern()
    for match in pattern.finditer(full_text):
        domain_key = taxonomy.domain_key_for_keyword(match.group(0))
        if domain_key is None:
            continue
        keyword_hits[domain_key] += 1
        if keyword_hits[domain_key] <= _MAX_KEYWORD_HITS:
            scores[domain_key] += W_DOMAIN_KEYWORD
            if len(evidence_by_domain[domain_key]) < 4:
                evidence_by_domain[domain_key].append(
                    Evidence(
                        kind="domain_keyword",
                        value=match.group(0),
                        snippet=snippet(full_text, match.start(), match.end()),
                        weight=W_DOMAIN_KEYWORD,
                    )
                )

    # Skills that signal a domain.
    for skill in skills:
        spec = taxonomy.SKILLS.get(skill.key)
        if spec is None or not spec.domains:
            continue
        # A skill shared across domains contributes proportionally less.
        share = W_DOMAIN_SKILL * skill.confidence / len(spec.domains)
        for domain_key in spec.domains:
            scores[domain_key] += share

    if not scores:
        return Detection(key=None, display=None, confidence=0.0)

    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    winner_key, winner_score = ranked[0]
    runner_up_score = ranked[1][1] if len(ranked) > 1 else 0.0

    domain = taxonomy.DOMAINS[winner_key]
    evidence = sorted(evidence_by_domain[winner_key], key=lambda e: -e.weight)[:6]

    if not evidence:
        evidence.append(
            Evidence(
                kind="domain_from_skills",
                value=domain.display_name,
                snippet=f"Inferred from extracted skills for {domain.display_name}.",
                weight=round(winner_score, 4),
            )
        )

    return Detection(
        key=winner_key,
        display=domain.display_name,
        confidence=_confidence(winner_score, runner_up_score),
        evidence=evidence,
        alternatives=[(key, round(score, 4)) for key, score in ranked[1:4]],
    )
