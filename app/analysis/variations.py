"""Same-role search title generation.

CLAUDE.md section 3 and section 18: only *same-role* variations may be
generated. For a Financial Analyst resume the system must never broaden to
Accountant, Auditor, Business Analyst, Data Analyst, Financial Advisor,
Investment Banker, Finance Manager, Sales Analyst or Tax Consultant.

That guarantee is structural, not heuristic. Output is built from exactly two
sources:

  1. the detected role's own ``variations`` list, which contains only
     re-phrasings of that one job
  2. seniority prefixes applied to those same titles

No other role's data is ever consulted, so an unrelated career cannot appear.
"""

from __future__ import annotations

from app.analysis import taxonomy
from app.analysis.text import normalize_title
from app.core.constants import Seniority

# Which prefix to add for a given seniority.
#
# MID gets none (a mid-level role is titled plainly). MANAGER, DIRECTOR and
# EXECUTIVE get none either: "Financial Analyst Manager" is not a real title,
# and prefixing would drift toward a different job.
_SENIORITY_PREFIXES: dict[Seniority, tuple[str, ...]] = {
    Seniority.INTERN: ("Intern",),
    Seniority.ENTRY: ("Entry Level", "Junior"),
    Seniority.JUNIOR: ("Junior",),
    Seniority.MID: (),
    Seniority.SENIOR: ("Senior",),
    Seniority.LEAD: ("Lead", "Senior"),
    Seniority.PRINCIPAL: ("Principal", "Senior"),
    Seniority.MANAGER: (),
    Seniority.DIRECTOR: (),
    Seniority.EXECUTIVE: (),
}

#: How many titles are stored on the profile. Deliberately above the
#: per-portal query cap in ``app.search.queries``, which is the bound that
#: actually limits request volume; this one only limits what is recorded.
DEFAULT_MAX_VARIATIONS = 12


def generate_role_variations(
    role_key: str,
    seniority: Seniority | None = None,
    *,
    max_variations: int = DEFAULT_MAX_VARIATIONS,
) -> list[str]:
    """Build the search titles for one detected role.

    The canonical title always comes first, then its seniority-prefixed
    forms, then the role's other phrasings. Callers cap this list, so the
    order decides what survives.
    """
    role = taxonomy.get_role(role_key)
    if role is None:
        return []

    ordered: list[str] = []
    seen: set[str] = set()

    def add(title: str) -> None:
        key = normalize_title(title)
        if key and key not in seen:
            seen.add(key)
            ordered.append(title)

    # 1. The canonical title, always first, so a portal that can serve only
    #    one query still searches the right thing.
    add(role.canonical_title)

    # 2. Seniority-prefixed forms of the canonical title, immediately after
    #    it. Only the canonical title is prefixed - prefixing every variation
    #    would produce noise like "Senior Finance Reporting Analyst".
    #
    #    Position matters. These used to be appended last, which meant the
    #    caller's cap truncated them first: across the taxonomy 21 roles lost
    #    their seniority query at some level, and for a senior Financial
    #    Analyst the dropped "Senior Financial Analyst" was measured, live, to
    #    be worth more qualifying jobs than the tail variations that displaced
    #    it. A seniority form is a re-phrasing of the canonical title itself,
    #    so it belongs beside it rather than behind the long tail.
    for prefix in _SENIORITY_PREFIXES.get(seniority, ()) if seniority else ():
        add(f"{prefix} {role.canonical_title}")

    # 3. The role's own same-role phrasings, in the order the taxonomy lists
    #    them - which is authored most-productive first for the same reason.
    for variation in role.variations:
        add(variation)

    return ordered[:max_variations]


def variations_are_same_role(role_key: str, variations: list[str]) -> bool:
    """Verify every variation still resolves to the same role, or to nothing.

    A prefixed form such as "Junior Financial Analyst" does not resolve
    directly, which is expected; what must never happen is a variation
    resolving to a *different* role.
    """
    for variation in variations:
        resolved = taxonomy.role_key_for_term(variation)
        if resolved is not None and resolved != role_key:
            return False
    return True
