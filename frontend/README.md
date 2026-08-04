# JobFinder — development dashboard (Phase 3.5)

A small React dashboard for **visually testing the Phase 1–3 backend**.

This is not the production UI. The production frontend is built separately in
Lovable; this exists so the backend can be exercised by hand — upload a resume,
watch the analysis, correct the detected role, inspect the data tables — without
reaching for `curl` or Swagger.

It has **no database, no backend of its own and no authentication**. Every value
on screen comes from the API.

---

## Running it

From the repository root, one command starts the backend and this dashboard
together, waits for both and opens the browser:

```powershell
.\start_app.ps1 -Install      # -Install only needed the first time
.\stop_app.ps1                # when you are done
```

To run the two halves yourself instead — the backend must be up first:

```bash
# 1. Backend, from the repository root
python -m uvicorn app.main:app --host 127.0.0.1 --port 8100

# 2. Dashboard, from frontend/
cd frontend
cp .env.example .env.local     # Windows: copy .env.example .env.local
npm install
npm run dev
```

Then open <http://localhost:5173>.

Other commands:

| Command | Purpose |
|---|---|
| `npm run dev` | Dev server with hot reload on port 5173 |
| `npm run build` | Production build into `dist/` |
| `npm run preview` | Serve the built output locally |

### Configuration

One environment variable, and nothing else:

```ini
VITE_API_BASE_URL=http://127.0.0.1:8100/api/v1
```

Include the `/api/v1` prefix and no trailing slash. The dashboard refuses to
start if it is missing, rather than silently calling the wrong host.

### Why port 5173 matters

The backend's `CORS_ALLOW_ORIGINS` already lists `http://localhost:5173`, so the
dev server is pinned to that port (`strictPort: true`). If you move it, add the
new origin to `CORS_ALLOW_ORIGINS` in the backend `.env` or the browser will
block every request — the dashboard will show "Backend unavailable" even though
the API is up.

---

## What it covers

| Section | Shows |
|---|---|
| **Backend status** | Health, per-dependency checks (database, Redis), and the constants from `/config` |
| **Resume management** | Upload PDF/DOCX/TXT, list resumes, pick the active one, duplicate handling, validation errors |
| **Resume analysis** | Domain and target role with confidence and evidence, seniority, years of experience, skills / technologies / tools, titles, education, certifications, responsibilities, same-role variations, warnings |
| **Profile correction** | Detected vs confirmed vs effective values, and a form to correct the domain and role |
| **Portals & search** | Portal list with enabled / implemented / selectable state, plus the disabled Find 20/50/100 controls |
| **Data foundation** | Read-only tables for search runs, jobs, applications and status history |

The **active resume** selected in Resume Management is what every other section
works against. It survives a page reload. Most Phase 3 endpoints are scoped to a
resume — `GET /jobs` refuses to run without one — because results and
application history never mix between resumes.

---

## Endpoints used

| Method | Endpoint | Used by |
|---|---|---|
| GET | `/health` | Backend status |
| GET | `/config` | Backend status |
| POST | `/resumes` | Resume management (upload) |
| GET | `/resumes` | Resume management (list) |
| POST | `/resumes/{id}/analyze` | Resume analysis |
| GET | `/resumes/{id}/profile` | Resume analysis |
| PATCH | `/resumes/{id}/profile` | Profile correction |
| GET | `/portals` | Portals |
| GET | `/search-runs` | Data foundation |
| GET | `/jobs` | Data foundation |
| GET | `/applications` | Data foundation |
| GET | `/application-history` | Data foundation |

`GET /resumes/{id}`, `GET /jobs/{id}`, `GET /search-runs/{id}`,
`GET /search-runs/{id}/progress` are wrapped in `src/api/endpoints.js` but not
yet bound to a view.

**Deliberately not called:** `POST /search-runs` and
`PATCH /applications/{job_id}`. Both work server-side, but Phase 3.5 stays
read-only apart from resume upload and profile correction.

---

## Structure

```
src/
├── api/
│   ├── client.js        fetch wrapper: envelope unwrapping, ApiError, network failures
│   └── endpoints.js     one function per backend endpoint
├── hooks/useAsync.js    loading / error / data, with request cancellation
├── state/ActiveResume.jsx   the shared resume selection
├── components/ui.jsx    Card, Badge, Table, and the loading/error/empty states
├── sections/            one file per dashboard section
├── App.jsx              sidebar navigation
└── styles.css           hand-written CSS, no UI framework
```

Every response uses the same envelope:

```json
{ "success": true, "data": {}, "message": null, "errors": [] }
```

so `src/api/client.js` unwraps it in one place and hands callers `data`.
Failures raise `ApiError`, which carries the backend's `errors[]` verbatim —
each entry has a dotted `field` such as `body.confirmed_domain`, which is what
lets the correction form show a message under the input that caused it.

---

## Deliberate omissions

Not built, because the corresponding backend phase does not exist:

- portal adapters, scraping or live search
- job matching, scoring or deduplication
- background workers

Not built, because the product never will:

- auto-apply, form filling, screening answers, resume auto-upload
- Apply/Submit buttons that post to this API
- Easy Apply or Quick Apply anything

The only apply action this product will ever have is opening a job URL in a new
tab so the user applies manually.

There are **no mock jobs and no simulated searches**. Where there is no data,
the dashboard says so.

The **full extracted resume text is never displayed** — the API does not expose
it, and the dashboard does not try to reconstruct it. The short evidence
snippets shown under a detection are fields the API returns on purpose.
