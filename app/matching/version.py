"""Matching-logic version, recorded on every ``job_matches`` row.

Scores are only comparable within one version. When the weights, the gates or
the extraction change, an old score no longer means what a new one means, so
the version stamped on the row is what makes a stored verdict interpretable
later (CLAUDE.md section 7).

Bump this whenever scoring output can change for any input:

  MAJOR  weights or components restructured; old scores are not comparable
  MINOR  new signal or taxonomy expansion; most scores unchanged
  PATCH  bug fix that changes output for some jobs
"""

from __future__ import annotations

SCORING_VERSION = "1.0.0"
