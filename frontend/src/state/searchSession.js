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
 * Permanent decisions are unaffected. Applied and Skipped are database-backed
 * and are read from the API every time; nothing here caches or overrides them.
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
