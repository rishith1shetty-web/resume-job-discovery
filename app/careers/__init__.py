"""Career-page discovery.

A deliberately separate package from ``app.portals``. A portal returns
individual job listings; this returns *companies* and the official career page
each one publishes. The two are different kinds of answer, so they get
different types, a different registry and - in the orchestrator - a different
lane. Nothing here produces a ``RawJob``, so nothing here can reach
normalisation, scoring, deduplication, ranking or the result allocation that
serves Find 20/50/100.

Importing this package registers the built-in sources.
"""

from __future__ import annotations

from app.careers import source as _source  # noqa: F401  (registers the source)
from app.careers.base import (
    CareerPage,
    CareerPageDiscovery,
    CareerPageRequest,
    CareerPageSource,
    CareerSourceRegistry,
    CompanyOpening,
    career_registry,
)
from app.careers.discovery import MAX_CAREER_PAGES, discover_career_pages
from app.careers.verification import VerifiedCareerUrl, verify_career_url

__all__ = [
    "MAX_CAREER_PAGES",
    "CareerPage",
    "CareerPageDiscovery",
    "CareerPageRequest",
    "CareerPageSource",
    "CareerSourceRegistry",
    "CompanyOpening",
    "VerifiedCareerUrl",
    "career_registry",
    "discover_career_pages",
    "verify_career_url",
]
