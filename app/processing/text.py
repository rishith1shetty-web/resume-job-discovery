"""Shared text cleaning used by every field normaliser.

Nothing here knows about jobs. It exists so "collapse the whitespace" means the
same thing for a title, a company and a location, and so the comparison keys
built from them are consistent.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

_WHITESPACE = re.compile(r"\s+")
# Everything whitespace-like except a newline, for block text.
_HORIZONTAL_SPACE = re.compile(r"[^\S\n]+")
_BLANK_RUN = re.compile(r"\n{3,}")

# Zero-width spaces and joiners, bidi marks, word joiner and the byte-order
# mark. Written as escapes because they are invisible in an editor: portals
# leak them out of rich-text editors and they would otherwise survive into a
# comparison key and stop two identical titles from matching.
_ZERO_WIDTH = re.compile("[​-‏⁠﻿]")

_PUNCTUATION = re.compile(r"[^\w\s]", flags=re.UNICODE)

# Bullets, pipes and dashes portals use as decoration around a value.
_EDGE_NOISE = re.compile(r"^[\s\-–—|/•*·,;:]+|[\s\-–—|/•*·,;:]+$")
_REPEATED_PUNCTUATION = re.compile(r"([\-–—|/•·])\1+")

#: Descriptions shorter than this are treated as a snippet, not a description.
#: Fingerprinting a teaser would make unrelated jobs look identical.
MIN_FINGERPRINT_LENGTH = 200


def clean_display_text(value: str | None) -> str | None:
    """Tidy a value for display without changing what it says.

    Trims, collapses runs of whitespace, drops invisible characters and strips
    decorative punctuation from the ends. It never re-cases, re-words or
    truncates: what the portal published is what the user sees.
    """
    if value is None:
        return None

    # NFKC folds non-breaking spaces and full-width characters into their
    # ordinary equivalents, so the whitespace collapse below catches them.
    text = unicodedata.normalize("NFKC", value)
    text = _ZERO_WIDTH.sub("", text)
    text = _WHITESPACE.sub(" ", text).strip()
    text = _REPEATED_PUNCTUATION.sub(r"\1", text)
    text = _EDGE_NOISE.sub("", text).strip()

    return text or None


def clean_block_text(value: str | None) -> str | None:
    """Tidy a multi-line block, keeping its structure.

    Used for descriptions, where ``clean_display_text`` would be wrong: it
    collapses newlines, and a description flattened into one paragraph is
    unreadable and loses the bullet lists the requirements live in. This trims
    each line, drops invisible characters and caps blank runs at one, and
    changes nothing else.
    """
    if value is None:
        return None

    text = unicodedata.normalize("NFKC", value)
    text = _ZERO_WIDTH.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    lines = [_HORIZONTAL_SPACE.sub(" ", line).strip() for line in text.split("\n")]
    block = "\n".join(lines).strip()
    block = _BLANK_RUN.sub("\n\n", block)

    return block or None


def comparison_key(value: str | None) -> str:
    """Reduce a value to something two portals can be compared on.

    Case, accents and punctuation all vary between portals for the same
    vacancy, so none of them survive. The result is only ever used for matching
    - it is never shown and never stored as the display value.
    """
    if value is None:
        return ""

    text = unicodedata.normalize("NFKD", value)
    # Strip accents: "Zurich" spelled either way is the same place.
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()
    text = _PUNCTUATION.sub(" ", text)

    return _WHITESPACE.sub(" ", text).strip()


def tokens(value: str | None) -> tuple[str, ...]:
    """Comparison key split into words."""
    key = comparison_key(value)
    return tuple(key.split()) if key else ()


def similarity(left: str | None, right: str | None) -> float:
    """How alike two strings are, from 0.0 to 1.0.

    Token-set Jaccard rather than an edit distance: portals reorder words and
    append qualifiers far more often than they misspell them, and word overlap
    survives both. Deterministic, and no dependency on a model.
    """
    left_tokens = set(tokens(left))
    right_tokens = set(tokens(right))

    if not left_tokens or not right_tokens:
        return 0.0
    if left_tokens == right_tokens:
        return 1.0

    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def fingerprint(text: str | None) -> str | None:
    """Stable hash of a description, for spotting the same posting copy.

    Returns None for anything too short to be distinctive. The hash is taken
    over the comparison key, so whitespace and punctuation differences between
    portals do not produce different fingerprints for identical text.
    """
    if not text:
        return None

    key = comparison_key(text)
    if len(key) < MIN_FINGERPRINT_LENGTH:
        return None

    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def stable_hash(*parts: str, length: int = 40) -> str:
    """Deterministic identifier built from already-normalised parts.

    Parts are joined with a separator that cannot occur in a comparison key, so
    ("ab", "c") and ("a", "bc") cannot produce the same hash.
    """
    payload = "␟".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]
