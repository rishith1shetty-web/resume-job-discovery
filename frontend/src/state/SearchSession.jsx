import { createContext, useCallback, useContext, useMemo, useState } from 'react'

import {
  clearedSession,
  EMPTY_SESSION,
  sessionFor,
  withCareerDecision,
  withError,
  withForm,
  withOutcome,
} from './searchSession.js'

/**
 * The last Find Matches result set, for as long as the app is open.
 *
 * # The problem this solves
 *
 * `Shell` renders one screen per route and mounts it under `key={route}`, so
 * navigating away from Find Matches unmounts it and React discards its state.
 * The user came back to an empty page and had to re-run a search that can take
 * minutes. This provider sits above that boundary, so the results survive
 * navigation.
 *
 * # Why memory, and nothing else
 *
 * A result set is **temporary session state**. It is held in `useState` and
 * written nowhere: no localStorage, no sessionStorage, no backend table. That
 * is what makes the lifetime correct without any expiry logic — closing the
 * app destroys the module instance, and the next launch constructs an empty
 * provider. Yesterday's search cannot come back because nothing kept it.
 *
 * `ActiveResume` deliberately does use localStorage, for the resume *id*. The
 * difference is intentional: which resume you are working on should survive a
 * reload; a page of search results should not.
 *
 * # What this does not touch
 *
 * Applied and Skipped are permanent, database-backed decisions. They are read
 * from the API on every render that needs them and are never cached here, so
 * application history and the server-side suppression of already-handled jobs
 * and career pages behave exactly as before.
 *
 * Nothing in this file starts a search. Returning to Find Matches shows what
 * was already found; running a new one stays an explicit user action.
 */

const SearchSessionContext = createContext(null)

export function SearchSessionProvider({ children }) {
  const [session, setSession] = useState(EMPTY_SESSION)

  const rememberOutcome = useCallback((resumeId, outcome) => {
    setSession((current) => withOutcome(current, { resumeId, outcome }))
  }, [])

  const rememberError = useCallback((resumeId, error) => {
    setSession((current) => withError(current, { resumeId, error }))
  }, [])

  const rememberForm = useCallback((resumeId, form) => {
    setSession((current) => withForm(current, { resumeId, ...form }))
  }, [])

  const rememberCareerDecision = useCallback((decision) => {
    setSession((current) => withCareerDecision(current, decision))
  }, [])

  const clearSession = useCallback(() => setSession(clearedSession()), [])

  /**
   * Read the remembered session for one resume.
   *
   * Isolation is enforced here rather than by clearing on switch: a result set
   * is only ever readable through the resume that produced it, so results
   * cannot leak into another resume even for one render.
   */
  const sessionForResume = useCallback(
    (resumeId) => sessionFor(session, resumeId),
    [session],
  )

  const value = useMemo(
    () => ({
      sessionForResume,
      rememberOutcome,
      rememberError,
      rememberForm,
      rememberCareerDecision,
      clearSession,
    }),
    [
      sessionForResume,
      rememberOutcome,
      rememberError,
      rememberForm,
      rememberCareerDecision,
      clearSession,
    ],
  )

  return (
    <SearchSessionContext.Provider value={value}>{children}</SearchSessionContext.Provider>
  )
}

export function useSearchSession() {
  const context = useContext(SearchSessionContext)
  if (!context) {
    throw new Error('useSearchSession must be used inside SearchSessionProvider.')
  }
  return context
}
