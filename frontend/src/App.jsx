/**
 * Rising Path Recruiters — application shell.
 *
 * Five screens, one navigation rail, one active resume shared across all of
 * them. Every developer-facing panel that used to live here (backend status,
 * portal diagnostics, data foundation) has been removed: this is the interface
 * the client uses, and a diagnostics tab in it would be an invitation to break
 * something.
 */

import { useEffect, useState } from 'react'

import { BrandLockup } from './components/Brand.jsx'
import { Icon } from './components/Icon.jsx'
import { useHashRoute } from './hooks/useHashRoute.js'
import { ApplicationHistory } from './pages/ApplicationHistory.jsx'
import { Dashboard } from './pages/Dashboard.jsx'
import { FindMatches } from './pages/FindMatches.jsx'
import { ResumeAnalysis } from './pages/ResumeAnalysis.jsx'
import { ResumeManagement } from './pages/ResumeManagement.jsx'
import { ActiveResumeProvider, useActiveResume } from './state/ActiveResume.jsx'
import { SearchSessionProvider } from './state/SearchSession.jsx'

const ROUTES = [
  {
    id: 'dashboard',
    label: 'Dashboard',
    icon: 'dashboard',
    title: 'Dashboard',
    lede: 'Your recruiting pipeline at a glance.',
    component: Dashboard,
  },
  {
    id: 'resumes',
    label: 'Resume Management',
    icon: 'document',
    title: 'Resume Management',
    lede: 'Upload candidate resumes and choose which one the platform works against.',
    component: ResumeManagement,
  },
  {
    id: 'analysis',
    label: 'Resume Analysis',
    icon: 'insight',
    title: 'Resume Analysis',
    lede: 'The structured profile extracted from the active resume, and the role it will be matched against.',
    component: ResumeAnalysis,
  },
  {
    id: 'find',
    label: 'Find Matches',
    icon: 'search',
    title: 'Find Matches',
    lede: 'Search every selected job board and return only genuine, US-based openings that match this resume.',
    component: FindMatches,
  },
  {
    id: 'history',
    label: 'Application History',
    icon: 'history',
    title: 'Application History',
    lede: 'Every application recorded against the active resume, kept separate from every other resume.',
    component: ApplicationHistory,
  },
]

const DEFAULT_ROUTE = 'dashboard'

export default function App() {
  return (
    <ActiveResumeProvider>
      {/* Above `Shell`, and therefore above the `key={route}` remount below:
          a Find Matches result set has to outlive navigation between tabs.
          It is memory-only, so it still dies with the session. */}
      <SearchSessionProvider>
        <Shell />
      </SearchSessionProvider>
    </ActiveResumeProvider>
  )
}

function Shell() {
  const [route, navigate] = useHashRoute(DEFAULT_ROUTE)
  const [railOpen, setRailOpen] = useState(false)

  const current = ROUTES.find((entry) => entry.id === route) ?? ROUTES[0]
  const Screen = current.component

  // Close the tablet drawer on navigation, and return focus to the top of the
  // new screen so a keyboard user is not left where the old page ended.
  useEffect(() => {
    setRailOpen(false)
    window.scrollTo({ top: 0, behavior: 'auto' })
  }, [route])

  useEffect(() => {
    if (!railOpen) return
    const onKey = (event) => event.key === 'Escape' && setRailOpen(false)
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [railOpen])

  return (
    <div className={`app-shell ${railOpen ? 'sidebar-open' : ''}`}>
      {railOpen && <div className="scrim" onClick={() => setRailOpen(false)} aria-hidden="true" />}

      <aside className="sidebar">
        <button
          type="button"
          onClick={() => navigate(DEFAULT_ROUTE)}
          style={{ display: 'block', width: '100%' }}
          aria-label={`${current.title} — go to dashboard`}
        >
          <BrandLockup />
        </button>

        <div className="sidebar-divider" />

        <nav className="sidebar-nav" aria-label="Primary">
          {ROUTES.map((entry) => (
            <button
              key={entry.id}
              type="button"
              className={`nav-item ${entry.id === route ? 'nav-item-active' : ''}`}
              onClick={() => navigate(entry.id)}
              aria-current={entry.id === route ? 'page' : undefined}
            >
              <Icon name={entry.icon} className="nav-icon" />
              {entry.label}
            </button>
          ))}
        </nav>

        <div className="sidebar-foot">
          <ActiveResumeRail onPick={() => navigate('resumes')} />
          <p className="sidebar-legal">
            © {new Date().getFullYear()} Rising Path Recruiters
            <br />
            Applications are submitted by you, never automatically.
          </p>
        </div>
      </aside>

      <div className="app-main">
        <header className="topbar">
          <button
            type="button"
            className="sidebar-toggle"
            onClick={() => setRailOpen((open) => !open)}
            aria-label="Toggle navigation"
            aria-expanded={railOpen}
          >
            <Icon name="menu" size={20} />
          </button>

          <div style={{ minWidth: 0 }}>
            <div className="topbar-title">{current.title}</div>
          </div>

          <div className="topbar-actions">
            <ThresholdChip />
          </div>
        </header>

        <main className="page" key={route}>
          <div className="animate-in">
            <Screen navigate={navigate} />
          </div>
        </main>
      </div>
    </div>
  )
}

/**
 * A permanent statement of the two rules that decide what this product will
 * ever show. Both are fixed in the backend; neither is a control.
 */
function ThresholdChip() {
  return (
    <span className="badge badge-accent" title="Fixed platform policy. Not adjustable.">
      <Icon name="shield" size={13} />
      70% minimum match · US roles only
    </span>
  )
}

function ActiveResumeRail({ onPick }) {
  const { activeResume, activeResumeId } = useActiveResume()

  return (
    <button type="button" className="rail-resume" onClick={onPick}>
      <span className={`rail-resume-dot ${activeResumeId ? '' : 'rail-resume-dot-idle'}`} />
      <span style={{ minWidth: 0, textAlign: 'left' }}>
        <span className="rail-resume-label">Active resume</span>
        <span className="rail-resume-name">
          {activeResumeId
            ? // After a reload only the id is known until the list is fetched.
              (activeResume?.original_filename ?? 'Selected resume')
            : 'None selected'}
        </span>
      </span>
    </button>
  )
}
