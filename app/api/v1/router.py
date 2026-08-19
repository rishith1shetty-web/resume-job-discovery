"""v1 API router.

Every router is mounted here so the version prefix stays in one place.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.routes import (
    applications,
    career_pages,
    config,
    health,
    jobs,
    matching,
    portals,
    resumes,
    search_runs,
    system,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(config.router)
api_router.include_router(resumes.router)
api_router.include_router(matching.router)
api_router.include_router(portals.router)
api_router.include_router(search_runs.router)
api_router.include_router(jobs.router)
api_router.include_router(applications.router)
api_router.include_router(career_pages.router)
api_router.include_router(system.router)
