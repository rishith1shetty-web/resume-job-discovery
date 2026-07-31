# CLAUDE.md

## Project
**Resume-Based Multi-Portal Job Discovery and Manual Application Tracking Platform**

## 1. Purpose
This is a new project, separate from the existing LinkedIn/Dice auto-apply system. A user uploads a resume; the system identifies the candidate's primary domain and one target role, searches selected job portals, finds the newest high-quality matches, returns job/application URLs, and stores resume-wise application history.

This project does **not** auto-apply. Do not build Easy Apply detection, form filling, resume upload automation, screening-answer automation, Apply/Submit clicking, or automatic submission. Users open URLs and apply manually.

## 2. Required User Flow
1. Upload PDF, DOCX, or TXT resume.
2. Parse it into a structured candidate profile.
3. Detect one primary domain and one target role.
4. Generate only same-role title variations for search.
5. Let the user select portals with checkboxes.
6. Let the user choose Find 20, Find 50, or Find 100.
7. Search every selected portal independently.
8. Search newest listings first within each portal; expand to older active jobs only when needed.
9. Normalize jobs, fetch full descriptions when available, validate URLs, score against the resume, reject weak/irrelevant jobs, and deduplicate across portals.
10. Return an approximately equal qualified share from each selected portal, then redistribute shortages only when necessary.
11. Rank final results by match quality and freshness.
12. Show job title, company, portal, score, posted date, location, status, and URL.
13. User applies manually and updates status.
14. Persist all search and application history by resume.

## 3. Resume Analysis
Create a structured profile containing:
- domain and one primary target role
- seniority and years of experience
- technical/domain skills, tools, technologies
- responsibilities and achievements
- education and certifications
- location, work-mode, and employment-type preferences when available

Example:
```text
Domain: Finance
Target role: Financial Analyst
```

Same-role search variations are allowed, for example:
```text
Financial Analyst
Junior Financial Analyst
FP&A Analyst
Financial Planning Analyst
Corporate Financial Analyst
Finance Reporting Analyst
```
Do not suggest or search unrelated careers merely because they share keywords. For a Financial Analyst resume, do not automatically broaden to Accountant, Auditor, Business Analyst, Data Analyst, Financial Advisor, Investment Banker, Finance Manager, Sales Analyst, or Tax Consultant.

The user may review/correct the detected domain and target role before searching, but the product should not present a broad “suggested roles” workflow.

## 4. Portal Model
Support at most 8 enabled portals at one time. The final portal list is not fixed; portals may be added, removed, enabled, or disabled later. Currently discussed possibilities include Jobright, HiringCafe, LinkedIn, Greenhouse, Glassdoor, Scale.jobs, ZipRecruiter, Insight Global, Robert Half, Remote Hunter, Qureos, and SimplyHired. Do not hardcode this list into the search engine.

Required design:
- `BasePortal` interface
- `PortalRegistry`
- one independent adapter per portal
- configuration-based enable/disable
- portal-specific timeout, retry, rate limit, logs, and error status
- failure isolation: one portal failure must not stop others

Each adapter must return the same normalized raw structure:
```python
@dataclass
class RawJob:
    external_id: str | None
    source: str
    title: str
    company: str | None
    location: str | None
    description: str | None
    source_url: str
    application_url: str | None
    posted_at: datetime | None
    employment_type: str | None
    raw_payload: dict
```

The central service must never contain a large `if portal == ...` chain.

## 5. Portal Selection and Result Limits
Dashboard controls:
```text
[ ] LinkedIn  [ ] Glassdoor  [ ] Greenhouse ...
[Find 20] [Find 50] [Find 100]
Minimum match: 70% (locked)
```
The selected portals and result limit are independent, but recommended use is:
- Find 20: about 2 portals
- Find 50: about 4 portals
- Find 100: all 8 portals

The user may still choose any enabled subset, such as LinkedIn + Glassdoor for Find 20.

Every selected portal must be searched. Do not stop after one portal produces enough jobs.

### Fair qualified-job quotas
Compute an approximate quota per selected portal:
```python
base = requested_limit // portal_count
remainder = requested_limit % portal_count
```
Examples:
- 20 across 2 portals: about 10 each
- 50 across 4 portals: 12–13 each
- 100 across 8 portals: 12–13 each

Quotas apply to **qualified final jobs**, not raw jobs. Never include a weak job just to satisfy a quota.

If one portal cannot fill its quota after all suitable active jobs are checked, redistribute only its unused slots to selected portals with additional qualified jobs. Record the shortage and redistribution in the search summary.

## 6. Freshness Policy
Within each selected portal, search progressively:
1. last 24 hours
2. last 3 days
3. last 7 days
4. last 14 days
5. last 30 days
6. older but still active jobs

Fill each portal's quota with the newest qualified jobs first. Expand to an older window only when the current windows do not provide enough qualified jobs. Do not reject a job solely because it is one day or one week old. Reject expired, unavailable, irrelevant, duplicate, or insufficiently matched jobs.

Store:
- `posted_at`
- `first_discovered_at`
- `last_seen_at`
- `last_verified_at`
- `expired_at`

“New” means first discovered in the current/new search window, not merely rediscovered.

## 7. Strict Matching
Only jobs for the detected primary domain and target role may be shown. A listing must pass:
- target-role/title gate
- complete-description analysis when available
- seniority and experience compatibility
- mandatory skill/qualification checks
- active URL validation
- final score >= 70%
- no critical rejection rule

Recommended weighted score:
- role/title alignment: 35%
- required skills: 25%
- experience/seniority: 15%
- responsibilities: 15%
- domain: 5%
- education/certification: 5%

Minimum gates:
```text
Role compatibility >= 80%
Mandatory skill coverage >= 60%
Experience compatibility >= 60%
Final weighted score >= 70%
```

Hard reject examples:
- wrong role family
- experience requirement substantially above candidate level
- missing mandatory certification, clearance, degree, license, or critical skill
- incompatible employment type/location when explicitly excluded
- missing/unverifiable description when reliable scoring is impossible
- expired listing or broken URL

Do not use plain keyword overlap or TF-IDF as the authoritative score. Use structured extraction, title normalization, deterministic rules, and semantic similarity. An LLM may assist extraction/reasoning, but deterministic validation remains authoritative. Store score components, rejection reasons, and `scoring_version`.

## 8. Ranking
Selection happens per portal first, then qualified portal results are combined.

Recommended portal ranking:
```text
80% resume match + 20% freshness
```
Recommended final dashboard ranking:
```text
75% resume match + 20% freshness + 5% source/direct-URL quality
```
Default sort: **Best Overall**. Also support Highest Match, Newest First, Portal, Company, and Status.

A one-week-old 94% match may rank above a five-minute-old 71% match. Freshness helps prioritize; it does not override relevance.

## 9. Deduplication and URL Preference
Do not deduplicate by URL alone. Use:
- external/ATS job ID
- canonicalized application URL
- normalized company + title + location
- description fingerprint

When the same job appears on several portals, store one canonical job plus all source records. Preferred URL order:
1. employer career page
2. official ATS application URL
3. original portal listing
4. aggregator redirect

Duplicate discovery must preserve prior application status. A job already marked Applied must not reappear as a new unapplied job through another portal.

## 10. Dashboard
Required top section:
- uploaded/active resume
- detected domain and target role
- portal checkboxes
- Find 20 / 50 / 100
- locked 70% threshold
- search progress and per-portal results/errors

Required result columns:
- title
- company
- portal/source(s)
- match score
- posted date
- location/work mode
- status
- direct job/application URL

Statuses:
- Not Applied
- Saved
- Applied
- Interview
- Rejected
- Offer
- Skipped
- Expired

Remove all auto-apply fields such as Easy Apply, Apply Priority, Unsupported Form, form progress, and auto-submit result.

## 11. Resume-Wise History
A user can upload multiple resumes. Search results, scores, and application history must never be mixed between resumes.

Required relationships:
- one user -> many resumes
- one resume -> one analyzed profile and many search runs
- one resume-job pair -> its own match score/components
- one application -> user + resume + canonical job
- status changes -> immutable history records

When a job is marked Applied, store `applied_at`. Future searches for the same resume must retain the status. Previously applied jobs may appear in history but should not count as newly discovered opportunities.

## 12. Core Database Tables
Use PostgreSQL + SQLAlchemy + Alembic. At minimum:
- `users`
- `resumes`
- `resume_profiles`
- `portals`
- `portal_configs`
- `search_runs`
- `search_run_portals`
- `search_queries`
- `raw_jobs`
- `jobs`
- `job_sources`
- `job_matches`
- `applications`
- `application_status_history`
- `portal_errors`

Important fields:
```text
search_runs: resume_id, requested_limit, min_score, status, started_at, completed_at
search_run_portals: portal_id, target_quota, raw_count, qualified_count, selected_count, status, error
jobs: canonical_key, title, normalized_title, company, normalized_company, location, description, posted_at, first_discovered_at, last_seen_at, last_verified_at, expired_at, preferred_application_url
job_matches: resume_id, job_id, search_run_id, final_score, role_score, skill_score, experience_score, responsibility_score, domain_score, education_score, freshness_score, ranking_score, qualified, rejection_reasons, scoring_version
applications: user_id, resume_id, job_id, status, applied_at, notes, created_at, updated_at
application_status_history: application_id, old_status, new_status, changed_at
```

## 13. API Surface
Suggested endpoints:
```text
POST   /resumes
POST   /resumes/{id}/analyze
GET    /resumes/{id}/profile
PATCH  /resumes/{id}/profile
GET    /portals
PATCH  /portals/{id}
POST   /search-runs
GET    /search-runs/{id}
GET    /search-runs/{id}/progress
GET    /jobs?resume_id=&search_run_id=&portal=&status=&sort=
GET    /jobs/{id}
PATCH  /applications/{job_id}
GET    /applications?resume_id=
GET    /application-history?resume_id=
```
`POST /search-runs` must accept `resume_id`, `selected_portal_ids`, and `requested_limit` (20, 50, or 100).

## 14. Background Processing
Portal collection, description retrieval, URL validation, matching, deduplication, and freshness checks should run as background jobs. Use Redis with Celery or Dramatiq. Track progress per portal and allow `completed_with_errors` when some portals fail.

## 15. Recommended Stack
- Python 3.12+
- FastAPI
- PostgreSQL, SQLAlchemy, Alembic
- Redis + Celery or Dramatiq
- Playwright only where necessary and permitted
- `pdfplumber`, `python-docx`
- sentence-transformers/embeddings for semantic comparison
- React/Next.js for production UI; Streamlit only for an early prototype
- Pytest
- Docker Compose
- structured logging, retries, timeouts, and error reporting

Respect portal terms, access restrictions, and rate limits. Do not claim complete coverage of every public job.

## 16. Search Algorithm
```text
Analyze active resume -> determine domain/target role -> generate same-role queries -> create search run -> calculate portal quotas -> run all selected portals independently -> collect newest candidates -> normalize/fetch descriptions -> role gate -> hard validation -> score -> reject below 70% -> deduplicate -> fill each portal quota -> expand freshness windows if needed -> redistribute genuine shortages -> combine -> rank -> persist -> display.
```

## 17. Development Order
Do not build all portals or the entire application at once.

### Phase 1: Foundation
Project structure, configuration, FastAPI, PostgreSQL, SQLAlchemy, Alembic, health endpoint, logging, tests.

### Phase 2: Resume Intelligence
Upload/parser, structured profile, domain/target-role detection, same-role query generation, profile review/edit.

### Phase 3: Data and Dashboard Foundation
Job/search/application models, basic dashboard, status history.

### Phase 4: Portal Framework
`BasePortal`, registry, configuration, one reliable adapter, portal contract tests.

### Phase 5: Job Processing
Normalization, canonical URLs, description retrieval, deduplication, URL validation, freshness fields.

### Phase 6: Matching
Role gate, mandatory requirements, weighted scoring, hard rejects, 70% filter, scoring explanations/tests.

### Phase 7: Multi-Portal Search
Checkbox selection, 20/50/100 limits, quotas, progressive freshness windows, redistribution, background progress, portal errors.

### Phase 8: Application Tracking
Manual statuses, `applied_at`, resume-wise history, filters, exports if needed.

### Phase 9: Remaining Portals
Add one adapter at a time. Each must pass contract/integration tests before enablement.

### Phase 10: Deployment and Hardening
Security, monitoring, rate limits, backups, migrations, performance, end-to-end tests.

## 18. Non-Negotiable Rules for Claude Code
- Inspect the repository before changing anything.
- Do not mix this project with the old auto-apply code.
- Do not alter or rewrite the original resume.
- Do not invent a fixed portal list.
- Do not build auto-apply features.
- Do not suggest unrelated roles.
- Search every selected portal.
- Apply result limits only after qualification and deduplication.
- Keep the 70% threshold locked; never lower it to fill quotas.
- Newest qualified jobs come first per portal; older active jobs are fallback.
- Preserve approximately equal portal coverage, then redistribute only genuine shortages.
- One portal failure must not fail the complete search.
- Store matching and application history by resume.
- Preserve previous Applied status across rediscovery and duplicates.
- Do not claim every job from every portal can be collected.
- Do not create large untested code dumps.
- Implement one approved phase at a time, run tests, report exact files changed, and stop for approval.

## 19. First Task
Before coding, provide:
1. repository assessment
2. final architecture and folder structure
3. database schema
4. API design
5. portal-adapter contract
6. resume-analysis format
7. strict matching and rejection logic
8. quota/freshness/redistribution algorithm
9. deduplication strategy
10. dashboard plan
11. phased implementation plan
12. contradictions, legal/access risks, and assumptions

Then implement **Phase 1 only**, run all tests, report commands/results and exact files created or modified, and wait for approval.
