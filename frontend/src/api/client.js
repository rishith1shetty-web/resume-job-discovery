/**
 * Reusable API client.
 *
 * Every backend response uses the same envelope:
 *
 *   { "success": true, "data": {}, "message": null, "errors": [] }
 *
 * so unwrapping it belongs in exactly one place. Callers get `data` and never
 * touch the envelope themselves.
 */

const RAW_BASE_URL = import.meta.env.VITE_API_BASE_URL

if (!RAW_BASE_URL) {
  throw new Error(
    'VITE_API_BASE_URL is not set. Copy frontend/.env.example to ' +
      'frontend/.env.local and restart the dev server.',
  )
}

/** Trailing slashes would produce `//resumes` when joined with a path. */
export const BASE_URL = RAW_BASE_URL.replace(/\/+$/, '')

/**
 * A failed request.
 *
 * `errors` is the backend's `errors[]` verbatim — each entry has `code`,
 * `field` (a dotted path such as `body.requested_limit`) and `message`, which
 * is what lets a form bind a message to the input that caused it.
 */
export class ApiError extends Error {
  constructor(message, { status = 0, errors = [], isNetworkError = false } = {}) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.errors = errors
    this.isNetworkError = isNetworkError
  }

  /** Message for one field, so a form can render it inline. */
  fieldError(field) {
    return this.errors.find((error) => error.field === field)?.message ?? null
  }
}

function buildUrl(path, query) {
  // The second argument is what lets BASE_URL be relative. The packaged
  // application serves the interface and the API from one origin, so its base
  // is `/api/v1`, and `new URL()` given a single relative string throws
  // `TypeError: Invalid URL`. A base is ignored when the first argument is
  // already absolute, so the development value still works unchanged.
  const url = new URL(`${BASE_URL}${path}`, window.location.origin)
  for (const [key, value] of Object.entries(query ?? {})) {
    // Skip unset filters rather than sending `?status=undefined`.
    if (value === undefined || value === null || value === '') continue
    url.searchParams.set(key, String(value))
  }
  return url.toString()
}

/**
 * Perform a request and return the whole envelope plus transport details.
 *
 * Used directly only by the health check, which must still show the database
 * status when the API answers 503. Everything else uses `request` below.
 */
export async function requestEnvelope(path, options = {}) {
  const { method = 'GET', query, body, formData, signal } = options

  const init = { method, signal, headers: {} }

  if (formData) {
    // Content-Type is deliberately unset: the browser adds the multipart
    // boundary, and setting it by hand breaks the upload.
    init.body = formData
  } else if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json'
    init.body = JSON.stringify(body)
  }

  // Built outside the try below on purpose. It used to be built inside it, so
  // a malformed base URL surfaced as "is the backend running?" — a question
  // about the server, when nothing had ever been sent to the server.
  const url = buildUrl(path, query)

  let response
  try {
    response = await fetch(url, init)
  } catch (cause) {
    // fetch only rejects for transport failures: server down, DNS, CORS.
    throw new ApiError(
      `Cannot reach the API at ${BASE_URL}. Is the backend running?`,
      { isNetworkError: true },
    )
  }

  let envelope = null
  try {
    envelope = await response.json()
  } catch {
    envelope = null
  }

  return { ok: response.ok, status: response.status, envelope }
}

/**
 * Perform a request and return `data`, throwing `ApiError` on failure.
 */
export async function request(path, options = {}) {
  const { ok, status, envelope } = await requestEnvelope(path, options)

  if (!ok || envelope?.success === false) {
    throw new ApiError(
      envelope?.message || `Request failed with HTTP ${status}.`,
      { status, errors: envelope?.errors ?? [] },
    )
  }

  if (envelope === null) {
    throw new ApiError(`The API returned a response that was not JSON (HTTP ${status}).`, {
      status,
    })
  }

  return envelope.data
}

/** Envelope `message` from a successful response, e.g. the duplicate notice. */
export async function requestWithMessage(path, options = {}) {
  const { ok, status, envelope } = await requestEnvelope(path, options)

  if (!ok || envelope?.success === false) {
    throw new ApiError(
      envelope?.message || `Request failed with HTTP ${status}.`,
      { status, errors: envelope?.errors ?? [] },
    )
  }

  return { data: envelope?.data ?? null, message: envelope?.message ?? null }
}
