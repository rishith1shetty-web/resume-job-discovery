"""Job processing: normalisation, canonicalisation and deduplication.

A leaf package, like ``app.portals``. Nothing here touches the database, the
network or a portal adapter: it turns a ``RawJob`` into normalised values and
decides whether two listings are the same vacancy. ``app.services.job_processing``
is the only place those decisions meet SQLAlchemy.

Two rules shape every module here:

* **Comparison never destroys display text.** Every normaliser returns both a
  cleaned value for the user to read and a key for machines to compare. The key
  is aggressive; the display value is not.
* **Uncertainty means "not a duplicate".** A false merge hides a real vacancy
  behind another and can carry a stale application status onto it. A missed
  merge only shows the user the same job twice, which they can see for
  themselves (CLAUDE.md section 9).
"""
