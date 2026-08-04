"""Find Matches search planning (Phase 7).

Four pure modules, none of which touch the database or name a portal:

``queries``     same-role query planning from one resume profile
``freshness``   progressive windows, and what "recent" means numerically
``collection``  driving one adapter through those windows, with isolation
``allocation``  fair per-portal shares, shortage redistribution and the
                deterministic final order

The database-facing workflow that strings these together lives in
``app.services.search_orchestrator``. Keeping the rules here means each one can
be tested on plain values, which is what makes "identical input produces an
identical result set" a property rather than a hope.
"""
