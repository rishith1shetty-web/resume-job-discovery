"""v1 API router.

Routers added in later phases (resumes, portals, search-runs, jobs,
applications) are mounted here so the prefix stays in one place.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.routes import config, health, resumes

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(config.router)
api_router.include_router(resumes.router)
