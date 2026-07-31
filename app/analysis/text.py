"""Text normalisation shared by every extractor.

Deterministic and dependency-free: the same input always produces the same
output, which is what makes analysis results reproducible and debuggable.
"""

from __future__ import annotations

import re
import unicodedata

# Characters that carry meaning inside skill and title names and must survive
# normalisation: C++, C#, .NET, Node.js, CI/CD, FP&A.
_KEEP = "+#./&"

_WHITESPACE_RE = re.compile(r"[^\S\n]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")
_BULLET_RE = re.compile(r"^\s*[•‣●○▪·*\-–—]\s+")

# Common ligatures and typographic characters produced by PDF extraction.
_REPLACEMENTS = {
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
    "–": "-",
    "—": "-",
    " ": " ",
    "ﬁ": "fi",
    "ﬂ": "fl",
    "…": "...",
}


def clean_text(raw: str) -> str:
    """Normalise extracted document text without discarding structure.

    Line breaks are preserved because section detection depends on them.
    """
    text = unicodedata.normalize("NFKC", raw)
    for source, target in _REPLACEMENTS.items():
        text = text.replace(source, target)

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Collapse runs of spaces/tabs but never newlines.
    text = _WHITESPACE_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


def strip_bullet(line: str) -> str:
    """Remove a leading bullet glyph from a resume line."""
    return _BULLET_RE.sub("", line).strip()


def normalize_token(value: str) -> str:
    """Fold a term to its comparison form.

    ``"Node.JS"`` and ``"node.js"`` compare equal; ``"C++"`` keeps its plus
    signs so it stays distinct from ``"C"``.
    """
    text = unicodedata.normalize("NFKC", value).lower().strip()
    text = "".join(
        char if (char.isalnum() or char in _KEEP or char.isspace()) else " "
        for char in text
    )
    return re.sub(r"\s+", " ", text).strip()


def normalize_title(value: str) -> str:
    """Fold a job title for comparison.

    Strips punctuation, seniority-neutral filler and employment-type suffixes
    so ``"Senior Financial Analyst II (Remote)"`` and ``"senior financial
    analyst"`` compare on the same basis.
    """
    text = normalize_token(value)
    text = re.sub(r"\b(i{1,3}|iv|v|\d+)\b$", "", text).strip()
    text = re.sub(
        r"\b(remote|hybrid|onsite|on site|contract|full time|part time|intern"
        r"ship|temporary|permanent|w2|c2c)\b",
        " ",
        text,
    )
    return re.sub(r"\s+", " ", text).strip()


def word_boundary_pattern(terms: list[str]) -> re.Pattern[str]:
    """Build one alternation regex matching any term on word boundaries.

    Terms are sorted longest-first so ``"machine learning"`` wins over
    ``"learning"``. Boundaries are custom rather than ``\\b`` because ``\\b``
    behaves wrongly next to ``+`` and ``#`` (``C++``, ``C#``).
    """
    if not terms:
        # A pattern that can never match, so callers need no special case.
        return re.compile(r"(?!x)x")

    ordered = sorted({term for term in terms if term}, key=len, reverse=True)
    alternation = "|".join(re.escape(term) for term in ordered)
    # The alternation MUST be wrapped in a group. Without it, `|` binds looser
    # than the lookarounds, so only the first term would get the lookbehind and
    # only the last would get the lookahead - every other term would then match
    # inside words ("ar" in "years", "r" in "corporate").
    return re.compile(
        rf"(?<![a-z0-9])(?:{alternation})(?![a-z0-9+#])", re.IGNORECASE
    )


def snippet(text: str, start: int, end: int, *, width: int = 60) -> str:
    """Return a short surrounding excerpt for evidence.

    Deliberately narrow: evidence must be enough to debug a detection without
    persisting large spans of private resume content.
    """
    left = max(0, start - width)
    right = min(len(text), end + width)
    excerpt = text[left:right].replace("\n", " ").strip()
    excerpt = re.sub(r"\s+", " ", excerpt)
    prefix = "..." if left > 0 else ""
    suffix = "..." if right < len(text) else ""
    return f"{prefix}{excerpt}{suffix}"
