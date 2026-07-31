"""Text normalisation and matching primitives."""

from __future__ import annotations

import pytest

from app.analysis.text import (
    clean_text,
    normalize_title,
    normalize_token,
    snippet,
    strip_bullet,
    word_boundary_pattern,
)


def test_clean_text_preserves_line_structure() -> None:
    # Section detection depends on newlines surviving normalisation.
    assert clean_text("A\r\nB\r\n\r\n\r\n\r\nC") == "A\nB\n\nC"


def test_clean_text_collapses_spaces_but_not_newlines() -> None:
    assert clean_text("a    b\n\nc") == "a b\n\nc"


def test_clean_text_normalises_typography() -> None:
    assert clean_text("“smart” – dash") == '"smart" - dash'


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("• Built models", "Built models"), ("- Led team", "Led team"), ("x", "x")],
)
def test_strip_bullet(raw: str, expected: str) -> None:
    assert strip_bullet(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("C++", "c++"),
        ("C#", "c#"),
        ("Node.js", "node.js"),
        ("FP&A", "fp&a"),  # ampersand must survive
        ("CI/CD", "ci/cd"),
        ("  Multiple   Spaces ", "multiple spaces"),
    ],
)
def test_normalize_token_keeps_meaningful_punctuation(raw: str, expected: str) -> None:
    assert normalize_token(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Senior Financial Analyst II", "senior financial analyst"),
        ("Data Analyst (Remote)", "data analyst"),
        ("Backend Engineer - Contract", "backend engineer"),
    ],
)
def test_normalize_title_strips_noise(raw: str, expected: str) -> None:
    assert normalize_title(raw) == expected


def test_word_boundary_pattern_matches_whole_terms_only() -> None:
    pattern = word_boundary_pattern(["r", "ar", "go", "sql"])

    assert pattern.search("R, SQL and Go") is not None
    # Regression: an ungrouped alternation let middle terms match inside words,
    # so "ar" matched "years" and "r" matched "corporate".
    assert pattern.search("six years of corporate experience") is None


def test_word_boundary_pattern_handles_plus_and_hash() -> None:
    pattern = word_boundary_pattern(["c", "c++", "c#"])

    assert pattern.search("C++ developer").group(0) == "C++"
    assert pattern.search("C# developer").group(0) == "C#"


def test_word_boundary_pattern_prefers_longest_match() -> None:
    pattern = word_boundary_pattern(["machine learning", "learning"])

    assert pattern.search("machine learning engineer").group(0) == "machine learning"


def test_empty_pattern_never_matches() -> None:
    assert word_boundary_pattern([]).search("anything at all") is None


def test_snippet_is_bounded_and_single_line() -> None:
    text = "line one\n" + ("x" * 500)
    result = snippet(text, 400, 410, width=20)

    assert "\n" not in result
    assert len(result) < 120
