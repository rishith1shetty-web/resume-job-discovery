import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'

import { getResume } from '../api/endpoints.js'

/**
 * The resume every other screen works against.
 *
 * Almost every endpoint in this product is scoped to one resume — `GET /jobs`
 * refuses to run without `resume_id` at all — so the selection is shared
 * rather than repeated per screen. It is kept in localStorage so a reload does
 * not lose it.
 *
 * The provider rehydrates the full record itself. Only the id survives a
 * reload, and if each screen had to look the name up separately then any
 * screen that did not happen to fetch the resume list would show "Selected
 * resume" in the navigation rail instead of the filename. Resolving it once,
 * here, means every screen gets the same answer.
 */

const STORAGE_KEY = 'jobfinder.activeResumeId'

const ActiveResumeContext = createContext(null)

export function ActiveResumeProvider({ children }) {
  const [activeResume, setActiveResumeState] = useState(null)
  const [activeResumeId, setActiveResumeIdState] = useState(() => {
    try {
      return localStorage.getItem(STORAGE_KEY)
    } catch {
      // Private-mode browsers can refuse storage; the app still works.
      return null
    }
  })

  const setActiveResume = useCallback((resume) => {
    setActiveResumeState(resume)
    setActiveResumeIdState(resume?.id ?? null)
    try {
      if (resume?.id) localStorage.setItem(STORAGE_KEY, resume.id)
      else localStorage.removeItem(STORAGE_KEY)
    } catch {
      /* ignore */
    }
  }, [])

  // Resolve the id into a record whenever the two disagree — on first load,
  // and after a reload.
  useEffect(() => {
    if (!activeResumeId || activeResume?.id === activeResumeId) return undefined

    let cancelled = false
    const controller = new AbortController()

    getResume(activeResumeId, controller.signal)
      .then((resume) => {
        if (!cancelled) setActiveResumeState(resume)
      })
      .catch((error) => {
        if (cancelled || controller.signal.aborted) return
        // Clear only when the resume is genuinely gone. A transient failure
        // must not wipe the user's selection — and an aborted request is not
        // a failure at all, though the client reports it as one, which is why
        // the signal is checked before the status.
        if (error?.status === 404) {
          setActiveResumeIdState(null)
          try {
            localStorage.removeItem(STORAGE_KEY)
          } catch {
            /* ignore */
          }
        }
      })

    return () => {
      cancelled = true
      controller.abort()
    }
  }, [activeResumeId, activeResume])

  const value = useMemo(
    () => ({ activeResume, activeResumeId, setActiveResume }),
    [activeResume, activeResumeId, setActiveResume],
  )

  return <ActiveResumeContext.Provider value={value}>{children}</ActiveResumeContext.Provider>
}

export function useActiveResume() {
  const context = useContext(ActiveResumeContext)
  if (!context) {
    throw new Error('useActiveResume must be used inside ActiveResumeProvider.')
  }
  return context
}
