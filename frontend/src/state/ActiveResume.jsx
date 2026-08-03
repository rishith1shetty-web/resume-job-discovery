import { createContext, useCallback, useContext, useMemo, useState } from 'react'

/**
 * The resume every other section works against.
 *
 * Almost every Phase 3 endpoint is scoped to one resume — `GET /jobs` refuses
 * to run without `resume_id` at all — so the selection is shared rather than
 * repeated per section. It is kept in localStorage so a page reload does not
 * lose it mid-test.
 */

const STORAGE_KEY = 'jobfinder.activeResumeId'

const ActiveResumeContext = createContext(null)

export function ActiveResumeProvider({ children }) {
  const [activeResume, setActiveResumeState] = useState(null)
  const [activeResumeId, setActiveResumeIdState] = useState(() => {
    try {
      return localStorage.getItem(STORAGE_KEY)
    } catch {
      // Private-mode browsers can refuse storage; the dashboard still works.
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

  const value = useMemo(
    () => ({ activeResume, activeResumeId, setActiveResume }),
    [activeResume, activeResumeId, setActiveResume],
  )

  return (
    <ActiveResumeContext.Provider value={value}>{children}</ActiveResumeContext.Provider>
  )
}

export function useActiveResume() {
  const context = useContext(ActiveResumeContext)
  if (!context) {
    throw new Error('useActiveResume must be used inside ActiveResumeProvider.')
  }
  return context
}
