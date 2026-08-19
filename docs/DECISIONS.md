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

---

## D-005 — LinkedIn and ZipRecruiter removed

**Status:** Recorded (2026-08-18). Implemented the same day.

Both portals are removed from the product entirely — adapter, registry entry,
seed, fixtures and tests. Neither is disabled-but-present, and neither leaves a
placeholder.

**LinkedIn.** Its `robots.txt` disallows automated access for this client
(`User-agent: * / Disallow: /`). The adapter was never permitted to collect and
never did; it sat behind an `access_authorized` option that no operator could
honestly set.

**ZipRecruiter.** Access was never the problem: the MCP endpoint ZipRecruiter
publishes in its own `/llms.txt` answers this project's user agent, read-only
and unauthenticated. The problem is that `search_jobs` is the only tool it
publishes, and its records carry no posting description — re-verified live on
2026-08-18, where the field set was byte-identical to the Phase 9B capture:

    benefits · company · company_logo · days_ago · is_remote
    job_redirect_url · job_type · location · salary · title

`CLAUDE.md` §7 rejects a posting that cannot be scored, so the portal qualified
nothing. Padding the description with salary and benefits would have cleared
that gate with text containing no requirements, responsibilities or skills —
which would not make a match real, only stop the pipeline from noticing it is
not. The manifest's newer `/Jobs/[Job-Title]` directory patterns do not reopen
the HTML surface: `llms.txt` defers permission to `robots.txt`, which grants
`/` only to a named allow-list this project is not on.

**What was deliberately kept**, because it is not about collecting from either
site:

- `linkedin.com` and `ziprecruiter.com` in `_AGGREGATOR_HOSTS`
  (`app/processing/urls.py`). That list names hosts a link may *point at* — it
  already includes `indeed.com`, `glassdoor.com`, `monster.com` and `dice.com`,
  none of which are portals here. Remaining portals surface such links, and
  dropping the entries would let an aggregator redirect outrank a real listing.
- LinkedIn Easy Apply detection in `app/processing/application_type.py`, for
  the same reason: other portals carry LinkedIn-sourced listings, and D-002
  requires those be excluded.
- `excludeLinkedIn=true` in the Scale.jobs adapter — a parameter that
  publisher's API defines.
- Career Pages refusing LinkedIn and ZipRecruiter company URLs as career pages.
- Every `linkedin_auto_apply` reference. That is the **separate** LinkedIn/Dice
  project this one must never collide with, and the guardrails naming it are
  load-bearing.

D-001 through D-004 are left as recorded. They are dated decisions, and the
policies in them — Easy Apply exclusion, duplicate handling, URL preference —
still govern the remaining portals even where LinkedIn was the example used to
state them.

**Removing the rows.** Dropping a slug from `PORTAL_SEEDS` stops a fresh
database growing the row but does nothing to an existing install, and
`GET /portals` selects every row — so a retired portal would keep appearing in
Find Matches. `RETIRED_PORTAL_SLUGS` in `app/services/portal_seed.py` is what
actually deletes it, through the existing seed script rather than a migration
(migrations carry schema; portal rows are environment data). It refuses to
delete a portal any job or search run still references, because that would
cascade into `job_sources` and destroy real history.
