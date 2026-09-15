# Frontend Integration Guide

For the React frontend built in **Lovable**. This backend is API-first: no
frontend code, templates or Streamlit dashboard live in this repository.

---

## 1. Base URL

| Environment | Base URL |
|---|---|
| Local development | `http://127.0.0.1:8100/api/v1` |
| Staging / production | TBD |

Port **8100** is deliberate — `8000` belongs to a separate project on the same
machine.

Put the base URL in an environment variable and never hardcode a host:

```ts
const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8100/api/v1";
```

Everything functional lives under `/api/v1`. Documentation routes (`/docs`,
`/redoc`, `/openapi.json`) sit at the root, outside the version prefix.

---

## 2. Response envelope

**Every** response — success and failure, every status code — has this shape:

```json
{
  "success": true,
  "data": {},
  "message": null,
  "errors": []
}
```

| Field | Type | Meaning |
|---|---|---|
| `success` | `boolean` | `false` for any 4xx/5xx |
| `data` | `object \| null` | Payload. `null` on error — see the `/health` exception below |
| `message` | `string \| null` | Optional human-readable summary |
| `errors` | `ErrorItem[]` | Empty on success |

So the client needs exactly one parsing path:

```ts
type ErrorItem = { code: string; field: string | null; message: string };

type Envelope<T> = {
  success: boolean;
  data: T | null;
  message: string | null;
  errors: ErrorItem[];
};

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });

  const body: Envelope<T> = await res.json();

  if (!res.ok || !body.success) {
    throw new ApiError(body.message ?? "Request failed", body.errors, res.status);
  }
  return body.data as T;
}
```

---

## 3. Error format

```json
{
  "success": false,
  "data": null,
  "message": "Validation failed.",
  "errors": [
    {
      "code": "greater_than_equal",
      "field": "body.requested_limit",
      "message": "Input should be greater than or equal to 20"
    }
  ]
}
```

`field` is a **dotted path** (`body.requested_limit`, `body.portals.0.id`,
`query.sort`), so messages bind straight to form inputs:

```ts
const fieldErrors = Object.fromEntries(
  errors
    .filter((e) => e.field?.startsWith("body."))
    .map((e) => [e.field!.slice(5), e.message]),
);
```

### Status codes

| Code | Meaning | Client action |
|---|---|---|
| 200 | Success | Render `data` |
| 201 | Created | Render `data` |
| 400 | Malformed request | Show `message` |
| 401 | Not authenticated | *(reserved; no auth in Phase 1)* |
| 403 | Not permitted | Show `message` |
| 404 | Not found | Show empty state |
| 409 | Conflict | Show `message` |
| 422 | Validation failed | Bind `errors[]` to fields |
| 500 | Internal error | Generic retry message |
| 503 | Dependency down | Retry with backoff |

Error responses never contain stack traces, SQL or connection strings.

---

## 4. Pagination, filtering and sorting

List endpoints (arriving Phase 3+) put a `Page` object in `data`:

```json
{
  "success": true,
  "data": {
    "items": [],
    "pagination": {
      "page": 1,
      "page_size": 25,
      "total": 0,
      "total_pages": 0,
      "has_next": false,
      "has_prev": false
    }
  },
  "message": null,
  "errors": []
}
```

This structure is fixed in Phase 1 so list endpoints added later do not change
the contract the frontend was built against.

Conventions:

- Page numbers are **1-based**; `page_size` max is 200.
- Query params are `snake_case`: `?page=2&page_size=50&sort=best_overall`
- Filters are explicit params: `?resume_id=…&search_run_id=…&portal=…&status=…`
- Sort values come from `GET /config` → `search.sort_options`.

---

## 5. CORS

Configured in `.env`:

```
CORS_ALLOW_ORIGINS=http://localhost:5173,http://localhost:3000
CORS_ALLOW_ORIGIN_REGEX=^https://.*\.lovable(project)?\.app$
CORS_ALLOW_CREDENTIALS=true
```

- Exact origins go in `CORS_ALLOW_ORIGINS`.
- Lovable preview subdomains rotate, so they are matched by
  `CORS_ALLOW_ORIGIN_REGEX`.
- Allowed methods: `GET, POST, PATCH, PUT, DELETE, OPTIONS`.
- `X-Request-ID` is in `expose_headers`, so JS can read it.
- A wildcard origin combined with credentials is **rejected at startup** rather
  than silently downgraded — add your deployed frontend origin explicitly.

---

## 6. Authentication assumptions

**Phase 1 ships no authentication.** All endpoints are open, and
`GET /config` reports `auth_required: false`.

The reserved scheme is a bearer token, so write the fetch wrapper once:

```
Authorization: Bearer <token>
```

When auth lands, `auth_required` flips to `true`, `auth_scheme` becomes
`"bearer"`, and unauthenticated calls return `401` in the standard envelope.
Build the client to read `auth_required` from `/config` rather than assuming.

> **Open decision:** the auth model (single-user local vs. multi-user hosted)
> has not been chosen. It should be settled before the frontend handles real
> user data.

---

## 7. Request correlation

Every response carries `X-Request-ID`. Send your own to correlate a failed call
with server logs:

```ts
headers: { "X-Request-ID": crypto.randomUUID() }
```

Include this value in bug reports — every server log line for that request
carries it.

---

## 8. Available endpoints (Phase 1)

### `GET /api/v1/health`

`200` + `success: true` when healthy, `503` + `success: false` when degraded.

> **The one exception to "`data` is null on error".** A degraded health response
> still returns a populated `data`, because the payload is the diagnosis — it
> names the dependency that failed. Call this endpoint directly rather than
> through the generic `apiFetch` wrapper, which throws on `success: false`.

```json
{
  "success": true,
  "data": {
    "status": "ok",
    "app_name": "JobFinder",
    "version": "0.1.0",
    "environment": "local",
    "api_version": "v1",
    "timestamp": "2026-07-24T12:00:00Z",
    "uptime_seconds": 12.5,
    "checks": [
      { "name": "database", "status": "up", "latency_ms": 1.8, "detail": null },
      { "name": "redis", "status": "not_configured", "latency_ms": null,
        "detail": "Reserved for background search workers (Phase 7)." }
    ]
  },
  "message": null,
  "errors": []
}
```

### `GET /api/v1/config`

Constants the dashboard renders from. **Read these instead of hardcoding
them** — it is what keeps the UI in step with locked backend rules.

```json
{
  "success": true,
  "data": {
    "app_name": "JobFinder",
    "version": "0.1.0",
    "environment": "local",
    "api_version": "v1",
    "api_prefix": "/api/v1",
    "auth_required": false,
    "auth_scheme": null,
    "search": {
      "result_limit_options": [20, 50, 100],
      "default_result_limit": 20,
      "min_match_score": 70,
      "min_match_score_locked": true,
      "max_enabled_portals": 16,
      "freshness_windows": [
        { "value": "last_24h", "label": "Last 24 hours" },
        { "value": "last_3d", "label": "Last 3 days" },
        { "value": "last_7d", "label": "Last 7 days" },
        { "value": "last_14d", "label": "Last 14 days" },
        { "value": "last_30d", "label": "Last 30 days" },
        { "value": "older_active", "label": "Older, still active" }
      ],
      "sort_options": [
        { "value": "best_overall", "label": "Best Overall" },
        { "value": "highest_match", "label": "Highest Match" },
        { "value": "newest_first", "label": "Newest First" },
        { "value": "portal", "label": "Portal" },
        { "value": "company", "label": "Company" },
        { "value": "status", "label": "Status" }
      ],
      "default_sort": "best_overall"
    },
    "resume": {
      "supported_extensions": [".pdf", ".docx", ".txt"],
      "supported_content_types": ["application/pdf", "…", "text/plain"],
      "max_upload_mb": 10
    },
    "application": {
      "statuses": [
        { "value": "not_applied", "label": "Not Applied" },
        { "value": "saved", "label": "Saved" },
        { "value": "applied", "label": "Applied" },
        { "value": "interview", "label": "Interview" },
        { "value": "rejected", "label": "Rejected" },
        { "value": "offer", "label": "Offer" },
        { "value": "skipped", "label": "Skipped" }
      ],
      "default_status": "not_applied"
    }
  },
  "message": null,
  "errors": []
}
```

---

### Resume endpoints (Phase 2)

| Method | Path | Notes |
|---|---|---|
| POST | `/api/v1/resumes` | `multipart/form-data`, field name **`file`** |
| GET | `/api/v1/resumes` | Paginated; `?page=&page_size=` |
| GET | `/api/v1/resumes/{id}` | Includes `has_profile` |
| POST | `/api/v1/resumes/{id}/analyze` | Creates a new profile version |
| GET | `/api/v1/resumes/{id}/profile` | Current profile |
| PATCH | `/api/v1/resumes/{id}/profile` | Confirm/correct domain and role |

**Upload.** Send `multipart/form-data` — do **not** set `Content-Type`
yourself; let the browser add the boundary:

```ts
const form = new FormData();
form.append("file", file);              // field name must be "file"
const res = await fetch(`${API_BASE_URL}/resumes`, { method: "POST", body: form });
```

Accepted: `.pdf`, `.docx`, `.txt`. Validation is by **magic bytes**, not the
filename, so renaming a `.png` to `.pdf` is rejected.

| Status | Meaning |
|---|---|
| 201 | Uploaded. `data.created` is `true` |
| 201 | Duplicate — `created: false`, `duplicate_of` set, existing resume returned |
| 413 | Larger than the configured limit |
| 415 | Unsupported type, or extension/content mismatch |
| 422 | Empty or unreadable file |

Duplicate uploads are **not** an error. Re-uploading identical bytes returns
the existing resume so a resume's history is never fragmented — render it as
"already uploaded", not a failure.

**Analyze.** Returns exactly **one** domain and **one** target role, each with
`confidence` (0–1) and `evidence[]`. Re-analysing creates a new version; the
previous one is retained. A scanned/image-only PDF returns **422** — surface
the message, since OCR is not supported.

**Profile.** Read `effective_domain` / `effective_target_role` — these hold the
user's correction when present and the detected value otherwise. Use
`role_variations` as the search titles; they are same-role only.

```jsonc
{
  "domain":      { "key": "finance", "display": "Finance", "confidence": 0.91, "evidence": [/* ... */] },
  "target_role": { "key": "financial_analyst", "display": "Financial Analyst", "confidence": 0.91, "evidence": [/* ... */] },
  "confirmed_target_role": null,
  "effective_target_role": "financial_analyst",
  "seniority": "senior",
  "years_experience": 7.4,
  "years_experience_source": "date_ranges",
  "role_variations": ["Financial Analyst", "FP&A Analyst", "Senior Financial Analyst"],
  "skills": [{ "skill_key": "fp&a", "display": "FP&A", "kind": "skill", "confidence": 1.0 }],
  "warnings": []
}
```

**Correction.** `PATCH` accepts `confirmed_domain` and/or
`confirmed_target_role` (at least one required). Values must be taxonomy keys,
not display names; an unknown key returns 422. The original detection and its
evidence are preserved, and changing the role regenerates `role_variations`.

UI notes:

- `years_experience` may be `null` — the resume gave nothing verifiable.
  Render "not stated", never `0`.
- `warnings[]` is advisory (e.g. no experience section found); show it as a
  hint, not an error.
- If `target_role.key` is `null`, prompt the user to set it before searching.
- Show `confidence` so a weak detection is visibly weak.
- **Resume text is never returned.** There is no endpoint that exposes it.

---

### Search, job and application endpoints (Phase 3)

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/portals` | Checkbox list; `?enabled_only=` |
| POST | `/api/v1/search-runs` | `resume_id`, `selected_portal_ids`, `requested_limit` |
| GET | `/api/v1/search-runs` | Paginated; `?resume_id=` |
| GET | `/api/v1/search-runs/{id}` | Run plus per-portal quotas |
| GET | `/api/v1/search-runs/{id}/progress` | Per-portal progress |
| GET | `/api/v1/jobs` | **`resume_id` required**; filters and sorts |
| GET | `/api/v1/jobs/{id}` | Description and every portal source |
| PATCH | `/api/v1/applications/{job_id}` | **`resume_id` required**; manual status |
| GET | `/api/v1/applications` | `?resume_id=&status=` |
| GET | `/api/v1/application-history` | `?resume_id=`; immutable, newest first |


These contracts are live and stable. What is **not** live yet is collection:
portal adapters arrive in Phase 4 and the background runner in Phase 7, so a
search run is recorded and stays `pending`, and `GET /jobs` returns whatever
has been stored so far. Build against these shapes now.

#### `GET /portals`

Portal checkboxes. Query `?enabled_only=true` for just the enabled ones.

A portal is only usable when **`is_selectable`** is true — that means enabled
*and* backed by a working adapter. Render the rest disabled rather than hiding
them, so the user can see what is coming.

#### `POST /search-runs`

```json
{
  "resume_id": "uuid",
  "selected_portal_ids": ["uuid", "uuid"],
  "requested_limit": 20
}
```

- `requested_limit` accepts **only** 20, 50 or 100. Anything else is a 422 on
  `body.requested_limit`.
- 1–16 portals, no repeats.
- `min_score` is **not** an input. It is always 70 and is returned read-only.
- 422 when the resume has not been analysed or has no target role — send the
  user to the profile screen rather than showing a generic error.

The response carries one entry per selected portal with its `target_quota`:
the limit split as evenly as possible, remainder to the first portals listed
(20 over 2 → 10/10; 50 over 4 → 13/13/12/12).

#### `GET /search-runs/{id}` and `/progress`

Poll `/progress` while a run is executing. Per-portal state is
`pending`, `running`, `completed`, `failed`, `skipped`; the run itself is
`pending`, `running`, `completed`, `completed_with_errors`, `failed` or
`cancelled`.

`completed_with_errors` is a **success** state: some portals failed, the rest
returned results. Render partial results plus per-portal errors — never treat it
as a total failure.

Counters narrow in this order, and the difference is worth showing:

| Field | Meaning |
|---|---|
| `raw_count` | everything the portal returned |
| `qualified_count` | what passed the 70% threshold |
| `selected_count` | what the quota actually took |
| `shortfall` | quota slots that could not be filled with qualified jobs |

A shortfall is never closed with a weaker job.

#### `GET /jobs?resume_id=…`

`resume_id` is **required**. Scores and application status belong to a
(resume, job) pair, so there is no correct way to list jobs without it.

Filters: `search_run_id`, `portal` (slug), `status`, `include_expired`
(default false). Sort: `best_overall` (default), `highest_match`,
`newest_first`, `portal`, `company`, `status`.

Each row carries `portals[]` — every portal that lists the job. A vacancy found
on two portals is **one row with two entries**, not two rows. `application_url`
is the most direct usable link; open it in a new tab and let the user apply.

#### `PATCH /applications/{job_id}?resume_id=…`

```json
{ "status": "applied", "notes": "optional" }
```

- Creates the record on first use, so no separate create call.
- `applied` stamps `applied_at`; moving on to `interview`, `rejected` or
  `offer` keeps that original timestamp.
- Re-sending the current status returns 200 with a `message` and writes no
  history entry — safe to fire on every click.
- There is no `expired` status; sending one is a 422.

#### `GET /applications` and `GET /application-history`

Both take an optional `resume_id`. History is newest-first and immutable: a
correction adds a transition rather than editing one.

---

## 9. Planned endpoints

| Endpoint | Phase |
|---|---|
| `PATCH /portals/{id}` (enable/disable, configure) | 4 |
| Search-run execution behind `POST /search-runs` | 7 |

---

## 10. UI rules the backend enforces

The frontend must not work around these; they are validated server-side.

1. **The 70% match threshold is locked.** Render it read-only. The backend
   rejects any attempt to lower it.
2. **Result limits are exactly 20, 50, 100.** Any other value is a 422.
3. **At most 16 portals enabled.** Selecting more is a 422.
4. **No auto-apply UI.** No Easy Apply badge, apply-priority column, form
   progress, auto-submit result or "Apply" button that posts to this API. The
   only apply action is opening the job URL in a new tab.
5. **Never show unrelated roles.** Results are for one detected domain and one
   target role. Do not add "similar roles" or "you might also like".
6. **`Expired` is a job property, not an application status.** A listing going
   stale must never overwrite a user's `Applied`. Render expiry as a separate
   badge.
7. **Per-portal errors are visible.** Show which portals failed and why.

---

## 11. Contract stability

- `/openapi.json` is the source of truth. Generate the client from it.
- Additive changes (new optional fields, new endpoints) ship within `v1`.
- Breaking changes go to a new prefix (`/api/v2`); `v1` is never changed in
  place.
- Enum values from `/config` are stable strings — key off `value`, display
  `label`.
