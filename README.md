# Resume-Based Multi-Portal Job Discovery Platform

Upload a resume, detect one primary domain and one target role, search selected
job portals for the newest strong matches, and track applications you submit
**by hand**.

This system **does not apply to jobs**. There is no Easy Apply handling, no form
filling, no resume-upload automation, no screening-answer automation and no
submission of any kind. It returns URLs; you apply and set the status yourself.

The authoritative specification is [CLAUDE.md](CLAUDE.md).

---

## Isolation from the LinkedIn/Dice auto-apply project

This project is **completely separate** from the existing auto-apply system and
shares nothing with it:

| Resource | This project | Never touched |
|---|---|---|
| Database | `resume_job_discovery` | `linkedin_auto_apply` |
| API port | `8100` | `8000` |
| Frontend port | `5173` (Vite dev) | — |
| Compose Postgres | `5433` | — |
| Compose Redis | `6380` | — |
| Storage | `./storage` | — |
| Migrations | `./migrations` (own history) | — |
| Config | this repo's `.env` | — |

Both projects can use the same local PostgreSQL **server** because they own
different **databases**. Nothing here reads, writes, migrates or inspects
`linkedin_auto_apply`, and no code was copied from the other project.

---

## Current status: Phase 3 complete (Job, Search and Application Data)

**Phase 1 — Foundation**

- FastAPI application with `/api/v1` versioning
- Configuration management, structured logging, central exception handling
- Async SQLAlchemy 2.0 + asyncpg, Alembic migrations
- Foundation tables: `users`, `resumes`, `portals`, `portal_configs`
- `GET /api/v1/health`, `GET /api/v1/config`, OpenAPI docs
- CORS for the Lovable React frontend
- Portal adapter **interfaces** (`BasePortal`, `PortalRegistry`) — no adapters

**Phase 2 — Resume Intelligence**

- Resume upload (PDF, DOCX, TXT) with magic-byte validation, filename
  sanitisation, size limits, SHA-256 checksums and duplicate handling
- Text extraction behind one interface: pdfplumber, python-docx, text decoding
- Deterministic extraction of skills, technologies, tools, titles,
  responsibilities, education, certifications, experience and seniority
- Detection of **exactly one** primary domain and **one** target role, with
  confidence scores and stored evidence
- Same-role search title generation only
- Tables `resume_texts`, `resume_profiles`, `resume_skills`; analysis is
  versioned so earlier detections stay traceable
- Six resume endpoints (see [API](#api))

**Phase 3 — Job, Search and Application Data**

- The remaining CLAUDE.md section 12 tables: `search_runs`,
  `search_run_portals`, `search_queries`, `raw_jobs`, `jobs`, `job_sources`,
  `job_matches`, `applications`, `application_status_history`, `portal_errors`
- One canonical job per vacancy with every portal source preserved, and the
  listing URL kept separate from the application URL
- Search runs with per-portal quotas (`requested_limit // portal_count`,
  remainder to the first portals), limits locked to 20/50/100 and the
  threshold locked at 70 — both as CHECK constraints, not just validators
- Manual application status keyed on (user, resume, job), so rediscovering a
  vacancy through a second portal resolves to the existing record instead of a
  new unapplied one
- Append-only status history; a repeated identical update writes nothing
- `Expired` is a job condition (`jobs.expired_at`), deliberately not an
  application status, so a stale listing cannot overwrite `Applied`
- Ten endpoints for portals, search runs, jobs and applications

Not implemented yet, by design: portal adapters and collection, job
normalisation and deduplication, the matching engine, background workers,
frontend code. A search run is recorded and stays `pending` until Phase 7. See
[Development order](#development-order).

### Resume analysis notes

- **No LLM is used.** Extraction is deterministic and works with no API key.
  `app/analysis/llm.py` defines an interface only; `enable_llm_assist` is
  false and the application refuses to start if it is turned on.
- **Nothing is inferred.** Experience, education and skills the resume does
  not support are left empty rather than guessed.
- **The original file is never modified.** Storage is content-addressed by
  checksum and existing files are never rewritten.
- **Resume text is never returned by the API.** It is stored for analysis and
  re-analysis only.

### Single-user mode

The app currently runs as a local single-user application: a default user is
created on first use and there is no authentication. The database keeps its
`user_id`-based architecture unchanged, so adding auth later means replacing
one dependency rather than redesigning the schema.

---

## Requirements

- Python 3.12
- PostgreSQL 16 (local server, or the bundled Docker Compose stack)
- Redis (only from Phase 7; provisioned in Compose now)

---

## Setup

### 1. Virtual environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

### 2. Configuration

```powershell
Copy-Item .env.example .env
```

Edit `.env` and set `POSTGRES_PASSWORD` at minimum. Defaults target the local
PostgreSQL 16 server on `127.0.0.1:5432`, database `resume_job_discovery`.

### 3. Database

**Option A — existing local PostgreSQL 16 server (default):**

```powershell
& "C:\Program Files\PostgreSQL\16\bin\psql.exe" -U postgres -h 127.0.0.1 `
    -c "CREATE DATABASE resume_job_discovery;"
```

This creates a new, empty database alongside any others on the server. It does
not modify existing databases.

**Option B — Docker Compose (isolated stack):**

```powershell
docker compose up -d
```

Publishes PostgreSQL on `127.0.0.1:5433` and Redis on `127.0.0.1:6380`, so it
cannot collide with a local install. If you use this, set `POSTGRES_PORT=5433`
in `.env`.

### 4. Migrations

```powershell
alembic upgrade head
```

### 5. Run

```powershell
uvicorn app.main:app --reload --port 8100
```

| URL | Purpose |
|---|---|
| http://127.0.0.1:8100/api/v1/health | Health check |
| http://127.0.0.1:8100/api/v1/config | Client configuration |
| http://127.0.0.1:8100/docs | Swagger UI |
| http://127.0.0.1:8100/redoc | ReDoc |
| http://127.0.0.1:8100/openapi.json | OpenAPI schema |

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/health` | Health check |
| GET | `/api/v1/config` | Constants the dashboard renders from |
| POST | `/api/v1/resumes` | Upload a resume (multipart) |
| GET | `/api/v1/resumes` | List resumes (paginated) |
| GET | `/api/v1/resumes/{id}` | Get one resume |
| POST | `/api/v1/resumes/{id}/analyze` | Analyze; creates a profile version |
| GET | `/api/v1/resumes/{id}/profile` | Get the analyzed profile |
| PATCH | `/api/v1/resumes/{id}/profile` | Confirm/correct domain and role |
| GET | `/api/v1/portals` | Portals for the checkbox list |
| POST | `/api/v1/search-runs` | Record a run and its per-portal quotas |
| GET | `/api/v1/search-runs` | List runs (paginated, `?resume_id=`) |
| GET | `/api/v1/search-runs/{id}` | Get one run |
| GET | `/api/v1/search-runs/{id}/progress` | Per-portal progress |
| GET | `/api/v1/jobs` | Jobs for one resume (`resume_id` required) |
| GET | `/api/v1/jobs/{id}` | One job with every portal source |
| PATCH | `/api/v1/applications/{job_id}` | Set the manual status |
| GET | `/api/v1/applications` | List applications |
| GET | `/api/v1/application-history` | Immutable status transitions |

`POST /search-runs` records the run and computes quotas; it does not collect
anything. Adapters land in Phase 4 and the background runner in Phase 7, so a
new run stays `pending`.

---

## Tests

```powershell
pytest              # full suite
pytest -q           # quiet
```

Unit tests (extraction, taxonomy, detection, validation) need no database.

Integration tests use **`resume_job_discovery`** — the same database as the
app, with these safeguards:

- A guard fixture asserts the configured database is exactly
  `resume_job_discovery` and **fails the run** otherwise. Any reference to
  `linkedin_auto_apply` aborts immediately.
- Each test runs inside a transaction that is **always rolled back**, so
  nothing is committed. The app's own `session.commit()` becomes a savepoint
  release via `join_transaction_mode="create_savepoint"`.
- Records carry an identifiable marker (`pytest+jobfinder@test.invalid`) as a
  second line of defence.
- Uploads are written to a pytest `tmp_path`, never the real `./storage`.
- Nothing truncates, drops, resets or recreates the database, and no
  destructive migration command is ever run.

If PostgreSQL is unreachable, integration tests skip with a clear reason
rather than failing the suite.

---

## Project layout

```
app/
├── main.py              app factory: middleware, CORS, routers, handlers
├── core/                config, logging, exceptions, error handlers, envelope
├── db/                  declarative base, async engine/session, mixins, types
├── models/              SQLAlchemy models
│   ├── user.py          users
│   ├── resume.py        resumes
│   ├── profile.py       resume_texts, resume_profiles, resume_skills
│   ├── portal.py        portals, portal_configs
│   ├── search.py        search_runs, search_run_portals, search_queries,
│   │                    portal_errors
│   ├── job.py           jobs, job_sources, raw_jobs, job_matches
│   └── application.py   applications, application_status_history
├── schemas/             Pydantic request/response schemas
├── api/
│   ├── deps.py          shared dependencies (session, settings, user, storage)
│   └── v1/routes/       health, config, resumes, portals, search-runs, jobs,
│                        applications
├── analysis/            deterministic resume analysis (no DB, no network)
│   ├── extraction/      PDF / DOCX / TXT behind one TextExtractor interface
│   ├── taxonomy/        domains, roles, skills (data + lookup)
│   ├── sections.py      section splitting
│   ├── skills.py        skill/technology/tool extraction
│   ├── titles.py        job-title extraction
│   ├── experience.py    date ranges -> years of experience
│   ├── education.py     education, certifications, responsibilities
│   ├── seniority.py     seniority detection
│   ├── detection.py     one domain + one target role, with evidence
│   ├── variations.py    same-role title generation
│   ├── pipeline.py      orchestration
│   ├── llm.py           interface only - disabled and unimplemented
│   └── version.py       ANALYSIS_VERSION
├── portals/             ISOLATED portal layer
│   ├── base.py          BasePortal, RawJob, PortalCapabilities
│   ├── registry.py      PortalRegistry
│   └── adapters/        empty until Phase 4
└── services/            storage, validation, resume/analysis/user services,
                         quota split, search-run/job/application services
migrations/              Alembic
tests/                   Pytest
docs/                    FRONTEND_INTEGRATION.md, DECISIONS.md
```

### Extending resume analysis

Detection quality is driven by data, not code. To improve coverage, add
entries to:

- `app/analysis/taxonomy/skills_data.py` — skills, technologies, tools
- `app/analysis/taxonomy/roles_data.py` — domains and roles

The taxonomy is validated at import: unknown domain references, unknown core
skills, ambiguous role aliases and seniority-prefixed variations all raise at
startup rather than degrading detection silently. Bump `ANALYSIS_VERSION` in
`app/analysis/version.py` whenever behaviour changes.

### Architectural rules

Dependencies point one way: `api → services → models`. The portal layer is a
leaf.

1. **Portals are data, not code.** The list lives in the `portals` table. The
   search service receives a `PortalRegistry` and iterates it. It never imports
   a concrete adapter and never contains `if portal == ...`.
2. **Failure is isolated.** One portal failing marks that portal's row with an
   error and lets the run finish as `completed_with_errors`.
3. **The 70% threshold is locked.** It can be raised, never lowered to fill a
   quota. Enforced by a validator in `app/core/config.py`.
4. **At most 8 portals enabled** at any time.
5. **No auto-apply concepts** anywhere — not in models, schemas or the OpenAPI
   document. Tests assert this.

---

## Frontend

The React frontend is built separately in Lovable and consumes this backend over
REST. No frontend code lives in this repository, and no Streamlit dashboard is
built.

See [docs/FRONTEND_INTEGRATION.md](docs/FRONTEND_INTEGRATION.md) for base URLs,
the response envelope, error format, pagination, CORS and auth assumptions.

---

## Development order

| Phase | Scope | Status |
|---|---|---|
| 1 | Foundation: structure, config, DB, health, logging, tests | **Complete** |
| 2 | Resume intelligence: parsing, profile, domain/role detection | **Complete** |
| 3 | Data foundation: job/search/application models and contracts | **Complete** |
| 4 | Portal framework: first adapter, contract tests | Not started |
| 5 | Job processing: normalisation, dedup, URL validation | Not started |
| 6 | Matching: role gate, weighted scoring, hard rejects | Not started |
| 7 | Multi-portal search: quotas, freshness, redistribution | Not started |
| 8 | Application tracking: statuses, history, filters | Not started |
| 9 | Remaining portals, one at a time | Not started |
| 10 | Deployment and hardening | Not started |

Each phase is implemented, tested and approved before the next begins.

---

## Legal and access note

Portal access is the highest-risk part of this system. Several portals prohibit
automated collection in their terms of service, and others may expose no usable
public listing surface. Adapters target public or official endpoints only. When
a portal blocks access it is marked unavailable rather than circumvented, and no
claim is made that every public job can be collected.
