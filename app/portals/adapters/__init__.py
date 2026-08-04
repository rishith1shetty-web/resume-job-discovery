"""Concrete portal adapters.

Importing this package is what populates the process-wide registry. The search
service asks the registry for adapters by slug and iterates them; it never
imports anything from this package directly, which is what keeps portal names
out of the core (CLAUDE.md section 4).

Phase 4 registers the first two, in the rollout order recorded in
docs/DECISIONS.md (D-001): Jobright, then LinkedIn. Every further portal is
added one at a time, after the existing ones are stable and tested.

No adapter may be enabled until ``portals.is_implemented`` is true, and being
implemented is not the same as being reachable — see the access notes in
``linkedin.py``.
"""

from app.portals.adapters.jobright import JobrightPortal
from app.portals.adapters.linkedin import LinkedInPortal
from app.portals.registry import registry

registry.register(JobrightPortal)
registry.register(LinkedInPortal)

__all__ = ["JobrightPortal", "LinkedInPortal"]
