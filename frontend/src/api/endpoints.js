/**
 * Every backend endpoint this dashboard calls, one function each.
 *
 * Paths, query parameters and field names are taken from the backend's
 * /openapi.json. Nothing here is invented: if a field is not in the schema,
 * the dashboard does not display it.
 *
 * System
 *   GET   /health
 *   GET   /config
 * Resumes
 *   POST  /resumes
 *   GET   /resumes
 *   GET   /resumes/{id}
 *   POST  /resumes/{id}/analyze
 *   GET   /resumes/{id}/profile
 *   PATCH /resumes/{id}/profile
 * Portals
 *   GET   /portals
 * Search runs
 *   POST  /search-runs
 *   GET   /search-runs
 *   GET   /search-runs/{id}
 *   GET   /search-runs/{id}/progress
 *   GET   /search-runs/{id}/results
 * Jobs
 *   GET   /jobs
 *   GET   /jobs/{id}
 * Applications
 *   GET   /applications
 *   GET   /application-history
 *
 * Deliberately NOT called: PATCH /applications/{job_id}. Editing an
 * application's status is Phase 8; this dashboard only reads it.
 *
 * Nothing here opens, fills or submits an application. Job links are rendered
 * for the user to open themselves.
 */

import { request, requestEnvelope, requestWithMessage } from './client.js'

// --- System ----------------------------------------------------------------

/**
 * Health, including the database check.
 *
 * Uses the raw envelope on purpose: a degraded backend answers 503 with
 * `success: false` but still reports which dependency is down, and that detail
 * is the whole point of this panel.
 */
export async function getHealth(signal) {
  const { ok, status, envelope } = await requestEnvelope('/health', { signal })
  return {
    reachable: true,
    httpStatus: status,
    healthy: ok && envelope?.success === true,
    payload: envelope?.data ?? null,
    message: envelope?.message ?? null,
  }
}

export function getConfig(signal) {
  return request('/config', { signal })
}

// --- Resumes ---------------------------------------------------------------

/**
 * Upload a resume. The field name must be `file` (multipart/form-data).
 *
 * Returns `{ resume, created, duplicate_of }`; `created: false` means
 * byte-identical content was already uploaded and the existing record was
 * returned instead of a duplicate.
 */
export function uploadResume(file, signal) {
  const formData = new FormData()
  formData.append('file', file)
  return requestWithMessage('/resumes', { method: 'POST', formData, signal })
}

export function listResumes({ page = 1, pageSize = 50 } = {}, signal) {
  return request('/resumes', {
    query: { page, page_size: pageSize },
    signal,
  })
}

export function getResume(resumeId, signal) {
  return request(`/resumes/${resumeId}`, { signal })
}

export function analyzeResume(resumeId, signal) {
  return requestWithMessage(`/resumes/${resumeId}/analyze`, {
    method: 'POST',
    signal,
  })
}

export function getProfile(resumeId, signal) {
  return request(`/resumes/${resumeId}/profile`, { signal })
}

/**
 * Correct the detected domain and/or target role.
 *
 * The backend stores these alongside the detected values rather than over
 * them, so the original detection and its evidence survive. At least one of
 * the two must be supplied.
 */
export function updateProfile(resumeId, { confirmedDomain, confirmedTargetRole }, signal) {
  const body = {}
  if (confirmedDomain !== undefined) body.confirmed_domain = confirmedDomain
  if (confirmedTargetRole !== undefined) body.confirmed_target_role = confirmedTargetRole

  return requestWithMessage(`/resumes/${resumeId}/profile`, {
    method: 'PATCH',
    body,
    signal,
  })
}

// --- Portals ---------------------------------------------------------------

export function listPortals({ enabledOnly = false } = {}, signal) {
  return request('/portals', {
    query: { enabled_only: enabledOnly ? 'true' : undefined },
    signal,
  })
}

/**
 * Exercise one adapter against the live portal.
 *
 * A diagnostic, not a search: the backend caps the limit and writes nothing.
 * A portal that refuses returns `ok: false` with the reason, rather than an
 * error status.
 */
export function probePortal(slug, { queries, limit = 3 }, signal) {
  return request(`/portals/${slug}/probe`, {
    method: 'POST',
    body: { queries, limit },
    signal,
  })
}

/**
 * Show what normalisation and deduplication would do with a small sample.
 *
 * Read-only: the backend issues only SELECTs, so nothing is stored. Listings
 * sharing a `group_index` would become one canonical job, and
 * `existing_job_id` means it would attach to a job already saved.
 */
export function previewProcessing(slug, { queries, limit = 5 }, signal) {
  return request(`/portals/${slug}/processing-preview`, {
    method: 'POST',
    body: { queries, limit },
    signal,
  })
}

// --- Search runs -----------------------------------------------------------

export function listSearchRuns({ resumeId, page = 1, pageSize = 25 } = {}, signal) {
  return request('/search-runs', {
    query: { resume_id: resumeId, page, page_size: pageSize },
    signal,
  })
}

export function getSearchRun(searchRunId, signal) {
  return request(`/search-runs/${searchRunId}`, { signal })
}

export function getSearchRunProgress(searchRunId, signal) {
  return request(`/search-runs/${searchRunId}/progress`, { signal })
}

/**
 * Find Matches: search every selected portal for one resume.
 *
 * Runs synchronously on the backend, so this request stays open for the whole
 * search. Returns the run, its per-portal summary and the qualified jobs.
 *
 * `requested_limit` accepts only 20, 50 or 100. The 70% threshold is locked
 * and is not part of the request: if fewer jobs qualify, the response reports
 * `is_short` rather than padding the list.
 */
export function startSearchRun({ resumeId, portalIds, limit }, signal) {
  return requestWithMessage('/search-runs', {
    method: 'POST',
    body: {
      resume_id: resumeId,
      selected_portal_ids: portalIds,
      requested_limit: limit,
    },
    signal,
  })
}

/** A completed run's results, in the order it returned them. */
export function getSearchRunResults(searchRunId, signal) {
  return request(`/search-runs/${searchRunId}/results`, { signal })
}

// --- Jobs ------------------------------------------------------------------

/** `resumeId` is required by the backend: scores and statuses are per resume. */
export function listJobs(
  { resumeId, searchRunId, portal, status, sort, includeExpired, page = 1, pageSize = 25 },
  signal,
) {
  return request('/jobs', {
    query: {
      resume_id: resumeId,
      search_run_id: searchRunId,
      portal,
      status,
      sort,
      include_expired: includeExpired ? 'true' : undefined,
      page,
      page_size: pageSize,
    },
    signal,
  })
}

export function getJob(jobId, resumeId, signal) {
  return request(`/jobs/${jobId}`, { query: { resume_id: resumeId }, signal })
}

// --- Applications ----------------------------------------------------------

export function listApplications({ resumeId, status, page = 1, pageSize = 25 } = {}, signal) {
  return request('/applications', {
    query: { resume_id: resumeId, status, page, page_size: pageSize },
    signal,
  })
}

export function listApplicationHistory({ resumeId, page = 1, pageSize = 50 } = {}, signal) {
  return request('/application-history', {
    query: { resume_id: resumeId, page, page_size: pageSize },
    signal,
  })
}
