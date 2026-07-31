"""Taxonomy invariants.

These guard the CLAUDE.md rule that only same-role titles are ever searched.
"""

from __future__ import annotations

import pytest

from app.analysis import taxonomy
from app.analysis.text import normalize_title


def test_taxonomy_loads() -> None:
    assert len(taxonomy.DOMAINS) >= 10
    assert len(taxonomy.ROLES) >= 40
    assert len(taxonomy.SKILLS) >= 150


def test_every_role_belongs_to_a_known_domain() -> None:
    for role in taxonomy.ROLES.values():
        assert role.domain_key in taxonomy.DOMAINS, role.key


def test_every_core_skill_exists() -> None:
    for role in taxonomy.ROLES.values():
        for skill_key in role.core_skills:
            assert skill_key in taxonomy.SKILLS, f"{role.key} -> {skill_key}"


def test_role_aliases_are_unambiguous() -> None:
    # A term claimed by two roles would make detection order-dependent.
    seen: dict[str, str] = {}
    for role in taxonomy.ROLES.values():
        for term in role.match_terms:
            assert term not in seen or seen[term] == role.key, term
            seen[term] = role.key


def test_canonical_title_is_always_a_variation() -> None:
    for role in taxonomy.ROLES.values():
        normalized = {normalize_title(v) for v in role.variations}
        assert normalize_title(role.canonical_title) in normalized, role.key


def test_variations_never_store_seniority_prefixes() -> None:
    # Seniority forms are generated at runtime; storing them would duplicate.
    for role in taxonomy.ROLES.values():
        for variation in role.variations:
            first_word = normalize_title(variation).split()[0]
            assert first_word not in taxonomy.SENIORITY_PREFIXES, variation


def test_no_variation_resolves_to_a_different_role() -> None:
    """The core same-role guarantee.

    If a role listed another job among its variations, this fails.
    """
    for role in taxonomy.ROLES.values():
        for variation in role.variations:
            resolved = taxonomy.role_key_for_term(variation)
            assert resolved in (None, role.key), (
                f"{role.key} variation {variation!r} resolves to {resolved!r}"
            )


@pytest.mark.parametrize(
    ("term", "expected"),
    [
        ("Financial Analyst", "financial_analyst"),
        ("FP&A Analyst", "financial_analyst"),
        ("Finance Reporting Analyst", "financial_analyst"),
        ("financial planning analyst", "financial_analyst"),
        # Adjacent but different jobs must resolve elsewhere.
        ("Accountant", "accountant"),
        ("Internal Auditor", "auditor"),
        ("Business Analyst", "business_analyst"),
        ("Data Analyst", "data_analyst"),
        ("Finance Manager", "finance_manager"),
        ("Financial Advisor", "financial_advisor"),
    ],
)
def test_role_term_resolution(term: str, expected: str) -> None:
    assert taxonomy.role_key_for_term(term) == expected


@pytest.mark.parametrize(
    ("term", "expected"),
    [
        ("Python", "python"),
        ("C++", "c++"),
        ("C#", "c#"),
        ("Node.JS", "node.js"),
        ("node js", None),  # not an alias; must not silently match
        ("financial planning & analysis", "fp&a"),
        ("Power BI", "power bi"),
        ("powerbi", "power bi"),
    ],
)
def test_skill_term_resolution(term: str, expected: str | None) -> None:
    assert taxonomy.skill_key_for_term(term) == expected


def test_finance_and_accounting_are_separate_domains() -> None:
    # CLAUDE.md forbids broadening a Financial Analyst to an Accountant.
    finance_roles = {role.key for role in taxonomy.roles_for_domain("finance")}
    accounting_roles = {role.key for role in taxonomy.roles_for_domain("accounting")}

    assert "financial_analyst" in finance_roles
    assert "accountant" in accounting_roles
    assert not finance_roles & accounting_roles
