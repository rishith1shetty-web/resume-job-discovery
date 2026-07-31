"""Resume-analysis logic version.

Every ``resume_profiles`` row records the version that produced it, so old
analyses stay traceable and a re-analysis under new logic never silently
overwrites the reasoning behind an earlier result.

Bump this whenever extraction, detection or scoring behaviour changes:

  MAJOR  incompatible profile shape
  MINOR  new extraction capability or taxonomy expansion
  PATCH  bug fix that changes output for some resumes
"""

from __future__ import annotations

ANALYSIS_VERSION = "1.0.0"
