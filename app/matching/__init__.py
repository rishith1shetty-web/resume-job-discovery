"""Resume-to-job matching: does this vacancy actually suit this candidate?

A leaf package, like ``app.portals`` and ``app.processing``. Nothing here
touches the database, the network or an LLM. ``app.services.match_service`` is
the only place these decisions meet SQLAlchemy.

Three rules shape every module:

* **Deterministic.** The same resume and the same job always produce the same
  score and the same explanation. No model, no randomness, no clock. Scoring
  is pure arithmetic over extracted facts, and ``ENABLE_LLM_ASSIST`` stays off
  (CLAUDE.md section 7).
* **The role gate comes first.** Skill overlap can never rescue an
  incompatible role. A Financial Analyst resume does not match an Accountant,
  a Business Analyst or a Data Analyst posting, however many tools they share
  (CLAUDE.md section 3).
* **Explainable.** Every point awarded and every point withheld carries a
  reason, and every rejection carries a code. A score with no explanation is
  not usable by someone deciding whether to spend an hour applying.

The 70% threshold is locked. It may be raised, never lowered to fill a result
count (CLAUDE.md section 18).
"""
