"""Business logic layer.

Empty in Phase 1 by design - no service is stubbed before the phase that
implements it. Planned modules, in build order:

  Phase 2  resume_service      upload, parse, structured profile
           profile_service     domain + single target role, same-role queries
  Phase 5  job_service         normalisation, canonical URLs, dedup
  Phase 6  matching_service    role gate, weighted score, hard rejects
  Phase 7  search_service      quotas, freshness escalation, redistribution
  Phase 8  application_service manual status changes, immutable history

``search_service`` will receive a ``PortalRegistry`` and iterate it. It must
never import a concrete adapter or branch on a portal name.
"""
