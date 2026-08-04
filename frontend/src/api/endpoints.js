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
 *   GET   /search-runs
 *   GET   /search-runs/{id}
 *   GET   /search-runs/{id}/progress
 * Jobs
 *   GET   /jobs
 *   GET   /jobs/{id}
 * Applications
 *   GET   /applications
 *   GET   /application-history
 *
 * Deliberately NOT called: POST /search-runs and PATCH /applications/{job_id}.
 * Phase 3.5 is read-only beyond resume upload and profile correction.
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
