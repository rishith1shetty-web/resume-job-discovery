"""Concrete portal adapters.

Importing this package is what populates the process-wide registry. The search
service asks the registry for adapters by slug and iterates them; it never
imports anything from this package directly, which is what keeps portal names
out of the core (CLAUDE.md section 4).

Phase 4 registered the first two, in the rollout order recorded in
docs/DECISIONS.md (D-001): Jobright, then LinkedIn. Phase 9A adds Hiring Cafe,
Phase 9B ZipRecruiter, Phase 9C SimplyHired, Phase 9D Greenhouse, Phase 9E
Scale.jobs, Phase 9F Dayforce, Phase 9G Google Careers. Every further portal is added one at a time, after the existing
ones are stable and tested.

No adapter may be enabled until ``portals.is_implemented`` is true, and being
implemented is not the same as being reachable — see the access notes at the
top of ``linkedin.py``, ``hiringcafe.py``, ``ziprecruiter.py``,
``simplyhired.py`` and ``scalejobs.py``.
"""

from app.portals.adapters.dayforce import DayforcePortal
from app.portals.adapters.google_careers import GoogleCareersPortal
from app.portals.adapters.greenhouse import GreenhousePortal
from app.portals.adapters.hiringcafe import HiringCafePortal
from app.portals.adapters.jobright import JobrightPortal
from app.portals.adapters.linkedin import LinkedInPortal
from app.portals.adapters.scalejobs import ScaleJobsPortal
from app.portals.adapters.simplyhired import SimplyHiredPortal
from app.portals.adapters.ziprecruiter import ZipRecruiterPortal
from app.portals.registry import registry

registry.register(JobrightPortal)
registry.register(LinkedInPortal)
registry.register(HiringCafePortal)
registry.register(ZipRecruiterPortal)
registry.register(SimplyHiredPortal)
registry.register(GreenhousePortal)
registry.register(ScaleJobsPortal)
registry.register(DayforcePortal)
registry.register(GoogleCareersPortal)

__all__ = [
    "DayforcePortal",
    "GoogleCareersPortal",
    "GreenhousePortal",
    "HiringCafePortal",
    "JobrightPortal",
    "LinkedInPortal",
    "ScaleJobsPortal",
    "SimplyHiredPortal",
    "ZipRecruiterPortal",
]
