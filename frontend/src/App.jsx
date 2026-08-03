import { useState } from 'react'

import { BASE_URL } from './api/client.js'
import { Analysis } from './sections/Analysis.jsx'
import { BackendStatus } from './sections/BackendStatus.jsx'
import { DataFoundation } from './sections/DataFoundation.jsx'
import { Portals } from './sections/Portals.jsx'
import { Resumes } from './sections/Resumes.jsx'
import { ActiveResumeProvider, useActiveResume } from './state/ActiveResume.jsx'

const SECTIONS = [
  { id: 'status', label: 'Backend status', component: BackendStatus },
  { id: 'resumes', label: 'Resume management', component: Resumes },
  { id: 'analysis', label: 'Resume analysis', component: Analysis },
  { id: 'portals', label: 'Portals & search', component: Portals },
  { id: 'data', label: 'Data foundation', component: DataFoundation },
]

export default function App() {
  return (
    <ActiveResumeProvider>
      <Shell />
    </ActiveResumeProvider>
  )
}

function Shell() {
  const [active, setActive] = useState(SECTIONS[0].id)
  const ActiveSection = SECTIONS.find((section) => section.id === active).component

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-name">JobFinder</span>
          <span className="brand-tag">Dev dashboard</span>
        </div>

        <nav className="nav">
          {SECTIONS.map((section) => (
            <button
              key={section.id}
              className={`nav-item ${section.id === active ? 'nav-item-active' : ''}`}
              onClick={() => setActive(section.id)}
              aria-current={section.id === active ? 'page' : undefined}
            >
              {section.label}
            </button>
          ))}
        </nav>

        <div className="sidebar-foot">
          <ActiveResumeChip />
          <div className="api-target">
            <span className="muted small">API</span>
            <code className="small">{BASE_URL}</code>
          </div>
        </div>
      </aside>

      <main className="main">
        <header className="main-head">
          <h1>{SECTIONS.find((section) => section.id === active).label}</h1>
          <p className="muted small">
            Development dashboard for testing the backend. Not the production UI.
          </p>
        </header>
        <ActiveSection />
      </main>
    </div>
  )
}

function ActiveResumeChip() {
  const { activeResume, activeResumeId } = useActiveResume()

  if (!activeResumeId) {
    return (
      <div className="active-chip active-chip-empty">
        <span className="muted small">No active resume</span>
      </div>
    )
  }

  return (
    <div className="active-chip">
      <span className="muted small">Active resume</span>
      <span className="strong small">
        {/* After a reload only the id is known until the list is fetched. */}
        {activeResume?.original_filename ?? 'Selected'}
      </span>
      <code className="small">{activeResumeId.slice(0, 8)}…</code>
    </div>
  )
}
