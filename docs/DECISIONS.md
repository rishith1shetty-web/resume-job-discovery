# Project Decisions

Binding decisions made outside `CLAUDE.md`, recorded so they survive across
sessions. `CLAUDE.md` remains the authoritative specification; nothing here
overrides it.

Status legend: **Recorded** = agreed, not yet implemented.

---

## D-001 — Portal rollout order

**Status:** Recorded (2026-07-31). Implementation begins in Phase 4.

Portals are enabled in a controlled sequence, not all at once:

1. **First: Jobright and LinkedIn only.**
2. Every other portal is added **one at a time**, and only after the existing
   adapters are stable, tested and approved.

This is a rollout *sequence*, not a hardcoded portal list. It does not conflict
with `CLAUDE.md` §4: the portal list still lives in the `portals` table, the
search service still resolves adapters through `PortalRegistry`, and no portal
name may appear in the central search service.

A portal stays `is_enabled = false` until its adapter passes contract tests.

---

## D-002 — Application-link rule

**Status:** Recorded (2026-07-31). Implementation spans Phases 4 and 5.

For every returned job the system provides:

- job title
- company
- portal source(s)
- match score
- posted date, when available
- a **usable job or application URL**

The user opens the URL and applies **manually**. The system never submits
anything.

### LinkedIn

- **Easy Apply jobs must be excluded.** They are filtered out of results
  entirely, because completing one requires in-platform submission, which this
  project must never perform.
- Normal external Apply links **are allowed**, including when they redirect to
  an employer careers page, Workday, Greenhouse, Lever or any other external
  application form. Following a redirect to show the user where to apply is
  not auto-applying.

### Jobright

- Return the usable job or application URL that the portal exposes.

### Never build

Restating `CLAUDE.md` §1 with the additions agreed in this decision:

- auto-apply
- form filling
- screening-answer automation
- automatic resume upload
- Apply or Submit clicking
- Quick Apply or one-click application logic
- Easy Apply detection used to *complete* an application (detection is
  permitted **only** to exclude such jobs from results)

---

## D-003 — Cross-portal duplicate handling

**Status:** Recorded (2026-07-31). Implementation begins in Phase 5.

When Jobright and LinkedIn both carry the same vacancy:

- store **one canonical job**
- **preserve both portal sources** as separate `job_sources` rows
- show the user **one result**
- prefer the **most direct usable application URL** (per `CLAUDE.md` §9:
  employer careers page > official ATS URL > original portal listing >
  aggregator redirect)
- preserve the **same resume-wise application status and history** — a job
  already marked Applied must never reappear as a new unapplied job because it
  was rediscovered through the other portal

---

## D-004 — Phase ownership

**Status:** Recorded (2026-07-31).

| Phase | Owns |
|---|---|
| 3 | Data models and API contracts **only** — no portal logic |
| 4 | Jobright and LinkedIn adapters |
| 5 | Normalization, preferred URL selection, duplicate merging |

Phase 3 must not implement portal collection, URL preference or deduplication
behaviour; it defines the tables and contracts those phases will populate.
