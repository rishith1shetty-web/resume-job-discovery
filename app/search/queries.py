"""Turning one analysed resume into the queries a portal will be asked.

CLAUDE.md sections 3 and 18: only the detected role and its *same-role* title
variations may be searched. For a Financial Analyst resume the system must
never issue a query for Accountant, Auditor, Business Analyst, Data Analyst,
Financial Advisor, Investment Banker, Finance Manager, Sales Analyst or Tax
Consultant.

That guarantee is inherited structurally rather than re-implemented: every
query comes from ``app.analysis.variations.generate_role_variations``, which
reads one role's own ``variations`` list and nothing else. This module adds
deduplication, a per-portal cap, and a defensive re-check that no generated
title resolves to a different role.

# Why skills and domain are not part of the query text

The brief allows adding "selected relevant skills only when useful". They are
deliberately not appended here, and the plan carries them as context instead.
Two reasons:

* Adapters map a query onto their own surface - Jobright resolves it to a
  taxonomy slug. "Financial Analyst SQL" matches no slug, so adding a skill
  makes a query *return nothing* rather than return better results.
* A skill-laden query narrows recall in a way that is invisible in the output:
  the jobs it never returned cannot be reported as rejected.

Skills and domain do their work in scoring, where their effect is explainable.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.analysis import taxonomy
from app.analysis.text import normalize_title
from app.analysis.variations import generate_role_variations
from app.core.logging import get_logger
from app.matching.profile import ResumeMatchProfile

logger = get_logger(__name__)

#: Queries issued to one portal in one run. Small on purpose: each one is a
#: separate request, and beyond a handful the variations stop describing the
#: same job and start describing an adjacent one.
DEFAULT_MAX_QUERIES_PER_PORTAL = 6


@dataclass(frozen=True, slots=True)
class QueryPlan:
    """The queries for one run, plus the context that produced them.

    Frozen and built only from the profile, so the same resume always yields
    the same plan - which is what makes a search run reproducible.
    """

    role_key: str
    role_display: str
    domain_key: str | None
    queries: tuple[str, ...]
    #: Titles that were generated but dropped because they resolved to a
    #: different role. Always empty in practice; recorded so a taxonomy edit
    #: that broke the guarantee would be visible rather than silent.
    rejected: tuple[str, ...] = ()

    def __len__(self) -> int:
        return len(self.queries)

    @property
    def is_empty(self) -> bool:
        return not self.queries


def build_query_plan(
    profile: ResumeMatchProfile,
    *,
    max_queries: int = DEFAULT_MAX_QUERIES_PER_PORTAL,
) -> QueryPlan:
    """Build the same-role query list for one resume.

    The canonical title always comes first, so a portal that can only serve one
    query still searches the right thing.
    """
    if max_queries < 1:
        raise ValueError("max_queries must be at least 1.")

    role_key = profile.target_role_key
    if not role_key:
        # A profile with no role must never reach a portal: with no role there
        # is no gate, and every job would look searchable.
        return QueryPlan(
            role_key="",
            role_display=profile.target_role_display or "",
            domain_key=profile.domain_key,
            queries=(),
        )

    generated = generate_role_variations(
        role_key, profile.seniority, max_variations=max_queries
    )

    ordered: list[str] = []
    rejected: list[str] = []
    seen: set[str] = set()

    for title in generated:
        # Defence in depth. generate_role_variations reads one role's own data,
        # so this cannot fire today; it would fire if a future taxonomy edit
        # put another role's title into this role's variations.
        resolved = taxonomy.role_key_for_term(title)
        if resolved is not None and resolved != role_key:
            rejected.append(title)
            logger.warning(
                "search_query_dropped_wrong_role",
                title=title,
                expected_role=role_key,
                resolved_role=resolved,
            )
            continue

        key = normalize_title(title)
        if not key or key in seen:
            continue
        seen.add(key)
        ordered.append(title)

    return QueryPlan(
        role_key=role_key,
        role_display=profile.target_role_display or role_key,
        domain_key=profile.domain_key,
        queries=tuple(ordered[:max_queries]),
        rejected=tuple(rejected),
    )


__all__ = ["DEFAULT_MAX_QUERIES_PER_PORTAL", "QueryPlan", "build_query_plan"]
