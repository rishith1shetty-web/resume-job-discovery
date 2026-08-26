/**
 * The rules governing a remembered Find Matches result set.
 *
 * Kept as plain functions with no React and no JSX so the two rules that
 * actually matter — a result set belongs to the resume that produced it, and
 * nothing outlives the session — can be tested directly with `node --test`
 * rather than asserted through a rendered component.
 *
 * There is no storage of any kind in this file. That is the design, not an
 * omission: a Find Matches result set is temporary session state, so it lives
 * in memory and dies with the page. Writing it to localStorage or the database
 * would make yesterday's search reappear tomorrow, which is precisely the
 * behaviour the product does not want.
 *
 * Permanent decisions are unaffected. Applied and Skipped are database-backed,
 * and `withPersistedStatuses` below is what keeps that true for a *remembered*
 * result set: the outcome held here is a snapshot taken when the search ran,
 * so the status inside it is only correct until the user changes one. On
 * restore the snapshot's statuses are overlaid with what the database
 * currently says, and the database wins.
 */

/** Nothing searched yet. The state a fresh session starts in. */
export const EMPTY_SESSION = Object.freeze({
  resumeId: null,
  outcome: null,
  error: null,
  /** Portal ids the user ticked, so the form is as they left it. */
  selectedPortalIds: null,
  /** 20 / 50 / 100. Null means "the page's own default". */
  limit: null,
  /**
   * Career pages acted on since this result set was produced, as
   * `officialCareerUrl -> {company, status}`.
   *
   * Remembered with the results and for the same reason: the cached outcome
   * still contains those cards, because the server only stops returning them
   * on the *next* search. Without this, navigating away and back would
   * resurrect cards the user had already dealt with.
   */
  decidedCareerPages: Object.freeze({}),
})

/**
 * The remembered session, but only when it belongs to `resumeId`.
 *
 * This is the resume-isolation rule, and it is expressed as a read rather than
 * as a cleanup: results are *never* shown for a resume that did not produce
 * them, whatever order the switch happened in. A stale entry left in memory
 * cannot leak, because reading it requires matching the current resume.
 */
export function sessionFor(session, resumeId) {
  if (!session || !resumeId) return EMPTY_SESSION
  if (session.resumeId !== resumeId) return EMPTY_SESSION
  return session
}

/** Whether a remembered result set is showable for this resume. */
export function hasResultsFor(session, resumeId) {
  return sessionFor(session, resumeId).outcome != null
}

/**
 * Record a completed search.
 *
 * Replaces whatever was there: running Find Matches again is the user asking
 * for a new answer, so the old result set goes rather than merging. The
 * career-page decisions go with it, because they described the old cards.
 */
export function withOutcome(session, { resumeId, outcome }) {
  return {
    ...(session ?? EMPTY_SESSION),
    resumeId,
    outcome,
    error: null,
    decidedCareerPages: {},
  }
}

/**
 * Record a failed search.
 *
 * The previous result set is dropped. Showing yesterday's results under
 * today's error message would be the worst of both: the user cannot tell which
 * search the rows belong to.
 */
export function withError(session, { resumeId, error }) {
  return {
    ...(session ?? EMPTY_SESSION),
    resumeId,
    outcome: null,
    error,
    decidedCareerPages: {},
  }
}

/**
 * Remember the form as the user left it, without touching the results.
 *
 * When the form belongs to a *different* resume than the stored one, the
 * stored results are dropped rather than carried across. Keeping them would
 * relabel one resume's result set as another's — the exact leak the isolation
 * rule exists to prevent, and the one way it could happen without anyone
 * reading a mismatched session.
 */
export function withForm(session, { resumeId, selectedPortalIds, limit }) {
  const base = session ?? EMPTY_SESSION
  const switching = resumeId != null && base.resumeId != null && resumeId !== base.resumeId
  const carried = switching ? EMPTY_SESSION : base

  return {
    ...carried,
    // The form belongs to whichever resume is being configured, even before a
    // search has run.
    resumeId: resumeId ?? base.resumeId,
    selectedPortalIds:
      selectedPortalIds === undefined ? carried.selectedPortalIds : selectedPortalIds,
    limit: limit === undefined ? carried.limit : limit,
    decidedCareerPages: carried.decidedCareerPages ?? {},
  }
}

/** Note that a career page was applied to or skipped. */
export function withCareerDecision(session, { officialCareerUrl, company, status }) {
  const base = session ?? EMPTY_SESSION
  return {
    ...base,
    decidedCareerPages: {
      ...base.decidedCareerPages,
      [officialCareerUrl]: { company, status },
    },
  }
}

/** Forget everything. Used when the active resume changes. */
export function clearedSession() {
  return { ...EMPTY_SESSION, decidedCareerPages: {} }
}


/**
 * `jobId -> status`, from the applications API's own records.
 *
 * Keyed on `job_id`, which is the identity the applications table itself uses
 * — `applications` is unique on `(user_id, resume_id, job_id)` — so this is
 * the same job identity the rest of the product tracks. No new identifier is
 * invented here, and nothing is matched on title, company or URL.
 *
 * Records without a job id or a status are skipped rather than guessed at: a
 * malformed row should cost that one row its overlay, not take the page down.
 */
export function statusMapFrom(applications) {
  const map = {}
  for (const record of applications ?? []) {
    const jobId = record?.job_id
    const status = record?.status
    if (typeof jobId === 'string' && typeof status === 'string') {
      map[jobId] = status
    }
  }
  return map
}

/**
 * A remembered outcome with each job's status refreshed from the database.
 *
 * This is the fix for a real bug: marking a job Applied, navigating to
 * Application History and coming back showed it as Not applied again. The
 * decision had been saved correctly — Application History proved it — but the
 * cached outcome still carried the status captured when the search ran, and
 * remounting the row re-seeded it from that stale snapshot.
 *
 * The overlay is deliberately one-directional. A job the user has never
 * tracked has no application record, so it keeps whatever the search said,
 * which is `not_applied`. A job that *does* have a record takes the record's
 * status, whether that decision was made on this page or on another one.
 *
 * Returns the outcome unchanged when there is nothing to apply, so a restore
 * that finds no applications does no work and creates no new objects.
 */
export function withPersistedStatuses(outcome, statusMap) {
  if (!outcome || !statusMap || Object.keys(statusMap).length === 0) return outcome

  const run = outcome.data ?? outcome
  const results = run.results ?? []
  if (results.length === 0) return outcome

  let changed = false
  const merged = results.map((job) => {
    const persisted = statusMap[job.job_id]
    if (!persisted || persisted === job.application_status) return job
    changed = true
    return { ...job, application_status: persisted }
  })

  if (!changed) return outcome

  const nextRun = { ...run, results: merged }
  // The outcome may arrive either bare or inside the API's `{data}` envelope,
  // and it is stored in whichever shape it arrived in. Rebuild the same shape
  // rather than normalising, so nothing downstream has to learn a new one.
  return outcome.data ? { ...outcome, data: nextRun } : nextRun
}


/**
 * How many applications one request asks for.
 *
 * Large enough that a resume's whole history is normally one round trip, small
 * enough to stay a reasonable response.
 */
export const APPLICATION_PAGE_SIZE = 200

/**
 * A ceiling on paging, so a backend that kept reporting "there is more" could
 * never spin here forever. At the page size above this is 20,000 applications
 * for one resume, which is far past any real use.
 */
export const MAX_APPLICATION_PAGES = 20

/**
 * The records and total in one `/applications` page.
 *
 * `listApplications` resolves to the *unwrapped* `data` — the API client
 * strips the `{success, data, message, errors}` envelope in exactly one place
 * — so a page is `{items, pagination}` and the records are at `page.items`.
 *
 * This function exists because getting that wrong is silent. A previous
 * attempt at this fix read `page.data.items`, unwrapping an envelope that had
 * already been unwrapped: `items` came back `undefined`, `?? []` turned it
 * into an empty list, the status map was empty, the overlay below found
 * nothing to apply, and every row rendered `Not applied` — the exact bug the
 * code was written to fix, with every test still passing because the tests
 * only ever exercised the merge and never the shape feeding it.
 *
 * So a shape that is not a page is an error rather than an empty result. An
 * empty list must mean "this resume has no applications", and nothing else.
 */
export function applicationPageRecords(page) {
  if (page === null || page === undefined) return { items: [], total: 0 }

  if (!Array.isArray(page?.items)) {
    throw new TypeError(
      'GET /applications did not return a page of records. Expected ' +
        '`{items, pagination}`, the unwrapped envelope data. Received keys: ' +
        `${Object.keys(page ?? {}).join(', ') || '(none)'}.`,
    )
  }

  const total = page.pagination?.total
  return {
    items: page.items,
    total: typeof total === 'number' ? total : page.items.length,
  }
}

/**
 * Every application record for one resume, following pagination to the end.
 *
 * `loadPage(pageNumber)` performs the request and resolves to whatever
 * `listApplications` resolves to. Passing the loader in rather than calling
 * the API here is what keeps this file free of transport and testable under
 * `node --test`, and it means the value this walks is the real one the
 * endpoint returns rather than a reshaped copy of it.
 *
 * Reads to the end deliberately. A partial read would drop the applications on
 * the later pages, and a dropped record shows as `Not applied` — silently
 * reintroducing the bug for exactly the resumes with the longest history.
 */
export async function collectApplicationRecords(
  loadPage,
  { maxPages = MAX_APPLICATION_PAGES } = {},
) {
  const records = []

  for (let page = 1; page <= maxPages; page += 1) {
    const { items, total } = applicationPageRecords(await loadPage(page))
    records.push(...items)
    // An empty page ends the walk whatever the total claims: without this a
    // backend reporting a total it cannot serve would page to the ceiling.
    if (items.length === 0 || records.length >= total) break
  }

  return records
}
