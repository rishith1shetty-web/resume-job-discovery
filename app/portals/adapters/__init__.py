"""Concrete portal adapters.

Importing this package is what populates the process-wide registry. The search
service asks the registry for adapters by slug and iterates them; it never
imports anything from this package directly, which is what keeps portal names
out of the core (CLAUDE.md section 4).

Phase 4 registered the first adapter, Jobright, in the rollout order recorded
in docs/DECISIONS.md (D-001). Phase 9A added Hiring Cafe, Phase 9C SimplyHired,
Phase 9D Greenhouse, Phase 9E Scale.jobs, Phase 9F Dayforce and Phase 9G Google
Careers. Every further portal is added one at a time, after the existing ones
are stable and tested.

LinkedIn and ZipRecruiter were removed in D-005: neither could supply what the
matching engine requires through a surface this project is permitted to read.
See that decision for the evidence; nothing here is a placeholder for them.

No adapter may be enabled until ``portals.is_implemented`` is true, and being
implemented is not the same as being reachable — see the access notes at the
top of ``hiringcafe.py``, ``simplyhired.py``, ``scalejobs.py``, ``dayforce.py``
and ``google_careers.py``.
"""

from app.portals.adapters.dayforce import DayforcePortal
from app.portals.adapters.google_careers import GoogleCareersPortal
from app.portals.adapters.greenhouse import GreenhousePortal
from app.portals.adapters.hiringcafe import HiringCafePortal
from app.portals.adapters.jobright import JobrightPortal
from app.portals.adapters.scalejobs import ScaleJobsPortal
from app.portals.adapters.simplyhired import SimplyHiredPortal
from app.portals.registry import registry

registry.register(JobrightPortal)
registry.register(HiringCafePortal)
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
    "ScaleJobsPortal",
    "SimplyHiredPortal",
]
