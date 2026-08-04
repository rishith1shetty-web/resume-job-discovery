"""Employer-name normalisation.

The risk here runs one way. Merging two employers that merely look alike
attaches a real vacancy to the wrong company and can carry an application
status with it, so the suffix list below is restricted to **legal entity
forms** - the part of a registered name that says nothing about which business
it is.

Words that look like suffixes but are part of the identity are deliberately
absent: "Group", "Holdings", "Partners", "Technologies", "Labs", "Systems",
"Solutions", "Ventures". "Apollo Group" and "Apollo Holdings" are not the same
employer, and nothing here will claim they are.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.processing.text import clean_display_text, comparison_key, similarity

# Legal entity forms only. Each of these can be added to or dropped from a
# company's name without changing which company it is.
_LEGAL_SUFFIXES = frozenset(
    {
        "inc",
        "incorporated",
        "llc",
        "l l c",
        "lc",
        "ltd",
        "limited",
        "co",
        "corp",
        "corporation",
        "plc",
        "llp",
        "lp",
        "gmbh",
        "mbh",
        "ag",
        "kg",
        "bv",
        "nv",
        "sa",
        "sas",
        "sarl",
        "srl",
        "spa",
        "ab",
        "as",
        "oy",
        "oyj",
        "aps",
        "pty",
        "pvt",
        "pte",
        "private",
        "sdn",
        "bhd",
        "kk",
        "jsc",
        "ao",
        "ooo",
    }
)

# Employer-branding phrases portals append to the company field.
_EMPLOYER_NOISE = re.compile(
    r"""
    \s*[\(\[]?\s*
    (?:
        via\s+\w+ | hiring\s+for | on\s+behalf\s+of |
        confidential | client\s+of\s+.* | recruit(?:ing|ment)\s+agency
    )
    \s*[\)\]]?\s*$
    """,
    flags=re.IGNORECASE | re.VERBOSE,
)

#: Company names must be this alike before a probable-duplicate merge.
#: Set high on purpose - see the module docstring.
COMPANY_SIMILARITY_THRESHOLD = 0.85


@dataclass(frozen=True, slots=True)
class NormalizedCompany:
    """An employer name split into what to show and what to compare on."""

    display: str
    key: str

    def __bool__(self) -> bool:
        return bool(self.key)


def normalize_company(company: str | None) -> NormalizedCompany | None:
    """Normalise an employer name, or return None when there is none.

    The display value keeps the legal suffix - "Acme Ltd" is how the employer
    writes itself, and dropping it from the results table would be editing the
    posting. Only the comparison key loses it.
    """
    display = clean_display_text(company)
    if not display:
        return None

    display = clean_display_text(_EMPLOYER_NOISE.sub("", display)) or display

    company_tokens = comparison_key(display).split()
    # Leading article: "The Home Depot" and "Home Depot" are one employer.
    if company_tokens and company_tokens[0] == "the":
        company_tokens = company_tokens[1:]

    # Strip trailing legal forms, repeatedly: "Acme Holdings Pvt Ltd".
    while len(company_tokens) > 1 and company_tokens[-1] in _LEGAL_SUFFIXES:
        company_tokens.pop()

    key = " ".join(company_tokens)
    if not key:
        # The name was nothing but a legal form. Compare on the original rather
        # than on an empty string, which would match every other empty key.
        key = comparison_key(display)

    return NormalizedCompany(display=display, key=key)


def companies_match(
    left: NormalizedCompany | None, right: NormalizedCompany | None
) -> tuple[bool, str]:
    """Is this the same employer? Returns (match, reason).

    A missing company on either side is *not* a match. Without an employer
    there is no way to tell two "Financial Analyst" postings apart, and
    guessing would merge unrelated vacancies.
    """
    if left is None or right is None:
        return False, "employer missing on one side"

    if left.key == right.key:
        return True, "identical normalised employer"

    # A division suffix: "Northside Hospital" vs "Northside Hospital Atlanta".
    # It must be a whole-token prefix, so "Apple" cannot swallow "Applebee's",
    # and the shorter name needs at least two tokens - a one-word prefix is far
    # too weak, and would merge "Square" into "Square Enix".
    left_tokens = left.key.split()
    right_tokens = right.key.split()
    shorter, longer = sorted((left_tokens, right_tokens), key=len)
    if len(shorter) >= 2 and longer[: len(shorter)] == shorter:
        return True, "one employer name extends the other"

    score = similarity(left.key, right.key)
    if score >= COMPANY_SIMILARITY_THRESHOLD:
        return True, f"employer names {score:.0%} similar"

    return False, f"different employers ({left.key!r} vs {right.key!r})"
