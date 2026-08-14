/**
 * Application History.
 *
 * Every application recorded against the *active* resume, and only that
 * resume. This isolation is the point of the screen rather than a detail of
 * it: two resumes may both be sent to the same company, and each must keep its
 * own record. The backend enforces it — `resume_id` is required on both
 * endpoints used here — and this page never merges results across resumes.
 *
 * Statuses are recorded by the user after they apply on the employer's own
 * site. Nothing here submits anything.
 */

import { useMemo, useState } from 'react'

import { listApplicationHistory, listApplications, listJobs } from '../api/endpoints.js'
import { STATUS_LABELS, StatusBadge } from '../components/ApplicationStatus.jsx'
import { Icon } from '../components/Icon.jsx'
import { useAsync } from '../hooks/useAsync.js'
import { useActiveResume } from '../state/ActiveResume.jsx'
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorState,
  FilterPill,
  PageHeader,
  Skeleton,
  StatCard,
  Table,
  formatDate,
  humanize,
  orDash,
  relativeTime,
} from '../components/ui.jsx'

/** The statuses that represent a real outcome, in pipeline order. */
const OUTCOMES = ['applied', 'interview', 'offer', 'rejected']

/**
 * Every status this page reads. Still the full set, on purpose: the table lists
 * whatever the user has actually recorded, so a row saved under Interview or
 * Offer must keep appearing under All with its own badge, and must keep getting
 * its board column enriched. Trimming this would hide existing records rather
 * than simplify the interface.
 */
const ALL_TRACKED = [...OUTCOMES, 'saved', 'skipped']

/**
 * The statuses this page offers as filter tabs, alongside All.
 *
 * Narrower than ALL_TRACKED, and deliberately so — it mirrors the three
 * decisions Find Matches lets the user make. Nothing is removed from the
 * status vocabulary, the API or the records; a tab that is not here simply
 * has no shortcut of its own, and its rows are still reachable under All.
 */
const FILTERS = ['applied', 'skipped']

export function ApplicationHistory({ navigate }) {
  const { activeResumeId, activeResume } = useActiveResume()
  const [filter, setFilter] = useState('all')
  const [search, setSearch] = useState('')

  const applications = useAsync(
    (signal) =>
      activeResumeId
        ? listApplications({ resumeId: activeResumeId, pageSize: 200 }, signal)
        : Promise.resolve(null),
    [activeResumeId],
    { immediate: Boolean(activeResumeId) },
  )

  // `ApplicationOut` carries the job title and company but not which board the
  // job came from, so the board is joined in from the jobs list.
  //
  // Queried once per tracked status rather than as one unfiltered request.
  // That is deliberate: an unfiltered `GET /jobs` also serialises *rejected*
  // matches, and the endpoint's response schema declares `rejection_reasons`
  // as a list of strings while the scorer stores objects — so the unfiltered
  // call returns 500. Restricting to statuses the user has actually recorded
  // returns only qualified jobs, which carry no rejection reasons, and is in
  // any case the only data this column needs.
  //
  // Still best-effort: the board is enrichment, not the record. A failure
  // costs the column a dash and leaves every application listed.
  const jobs = useAsync(
    async (signal) => {
      if (!activeResumeId) return null
      const pages = await Promise.all(
        ALL_TRACKED.map((status) =>
          listJobs({ resumeId: activeResumeId, status, pageSize: 100 }, signal).catch(
            () => null,
          ),
        ),
      )
      return pages.filter(Boolean).flatMap((page) => page.items ?? [])
    },
    [activeResumeId],
    { immediate: Boolean(activeResumeId) },
  )

  const portalsByJob = useMemo(() => {
    const map = {}
    for (const job of jobs.data ?? []) map[job.id] = job.portals ?? []
    return map
  }, [jobs.data])

  const items = applications.data?.items ?? []

  const counts = useMemo(() => {
    const tally = { all: items.length }
    for (const status of ALL_TRACKED) tally[status] = 0
    for (const item of items) {
      if (tally[item.status] !== undefined) tally[item.status] += 1
    }
    return tally
  }, [items])

  const visible = useMemo(() => {
    const term = search.trim().toLowerCase()
    return items
      .filter((item) => filter === 'all' || item.status === filter)
      .filter(
        (item) =>
          !term ||
          (item.job_title ?? '').toLowerCase().includes(term) ||
          (item.company ?? '').toLowerCase().includes(term),
      )
      .sort(
        (a, b) =>
          new Date(b.status_changed_at ?? b.updated_at) -
          new Date(a.status_changed_at ?? a.updated_at),
      )
  }, [items, filter, search])

  if (!activeResumeId) {
    return (
      <div className="stack">
        <PageHeader title="Application History" />
        <Card>
          <EmptyState
            icon="history"
            title="No active resume"
            hint="History is kept separately for each resume. Choose one to see its applications."
          >
            <Button variant="primary" icon="document" onClick={() => navigate('resumes')}>
              Choose a resume
            </Button>
          </EmptyState>
        </Card>
      </div>
    )
  }

  const loading = applications.loading

  return (
    <div className="stack">
      <PageHeader
        title="Application History"
        lede={
          <>
            Applications recorded against{' '}
            <strong>{activeResume?.original_filename ?? 'the active resume'}</strong>. Other resumes
            keep their own separate history.
          </>
        }
        actions={
          <Button variant="ghost" icon="refresh" onClick={applications.run} disabled={loading}>
            Refresh
          </Button>
        }
      />

      {/* One card now, so the column track is pinned locally. `.grid-stats` is
          `repeat(auto-fit, minmax(200px, 1fr))`, which would stretch a lone
          card the full width of the page — a single number in a banner. The
          override is inline rather than in layout.css because every other
          page still wants the shared four-across behaviour. */}
      <section className="grid grid-stats" style={{ gridTemplateColumns: 'minmax(220px, 320px)' }}>
        <StatCard
          label="Applications"
          value={counts.applied}
          loading={loading}
          icon="send"
          tone="info"
          hint="Submitted by you"
        />
      </section>

      <Card flush>
        <div className="toolbar">
          <FilterPill active={filter === 'all'} count={counts.all} onClick={() => setFilter('all')}>
            All
          </FilterPill>
          {FILTERS.map((status) => (
            <FilterPill
              key={status}
              active={filter === status}
              count={counts[status]}
              onClick={() => setFilter(status)}
            >
              {STATUS_LABELS[status]}
            </FilterPill>
          ))}

          <span className="spacer" />

          <label className="input-group" style={{ flex: '0 1 240px', minWidth: 180 }}>
            <span className="sr-only">Search by job title or company</span>
            <input
              className="input"
              type="search"
              placeholder="Search job or company…"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
          </label>
        </div>

        {loading ? (
          <div style={{ padding: 'var(--space-6)' }}>
            <Skeleton count={6} height={22} />
          </div>
        ) : applications.error ? (
          <div style={{ padding: 'var(--space-6)' }}>
            <ErrorState error={applications.error} onRetry={applications.run} />
          </div>
        ) : items.length === 0 ? (
          <EmptyState
            icon="history"
            title="Nothing recorded for this resume yet"
            hint="Find matches, apply on the employer's own site, then set each job's status. It will appear here."
          >
            <Button variant="primary" icon="search" onClick={() => navigate('find')}>
              Find matches
            </Button>
          </EmptyState>
        ) : visible.length === 0 ? (
          <EmptyState
            icon="filter"
            title="No applications match this filter"
            hint="Try a different status, or clear the search box."
          >
            <Button
              variant="default"
              size="sm"
              onClick={() => {
                setFilter('all')
                setSearch('')
              }}
            >
              Clear filters
            </Button>
          </EmptyState>
        ) : (
          <Table
            columns={['Job', 'Company', 'Board', 'Date applied', 'Last updated', 'Status', 'Notes']}
            rows={visible}
            renderRow={(item) => (
              <tr key={item.id}>
                <td style={{ maxWidth: 300 }}>
                  {item.application_url ? (
                    <a href={item.application_url} target="_blank" rel="noreferrer noopener">
                      {item.job_title ?? 'Untitled role'}
                    </a>
                  ) : (
                    <span className="strong">{item.job_title ?? 'Untitled role'}</span>
                  )}
                </td>
                <td>{orDash(item.company)}</td>
                <td>
                  <div className="chip-row">
                    {(portalsByJob[item.job_id] ?? []).map((slug) => (
                      <Badge key={slug} tone="neutral">
                        {humanize(slug)}
                      </Badge>
                    ))}
                    {(portalsByJob[item.job_id] ?? []).length === 0 && orDash(null)}
                  </div>
                </td>
                {/* applied_at is stamped server-side only when the status
                    becomes Applied, so a Saved or Skipped row legitimately
                    has none. */}
                <td className="small">{orDash(formatDate(item.applied_at))}</td>
                <td className="small subtle">
                  {relativeTime(item.status_changed_at ?? item.updated_at)}
                </td>
                <td>
                  <StatusBadge status={item.status} />
                </td>
                <td style={{ maxWidth: 240 }}>
                  {item.notes ? (
                    <span className="small clamp-2" title={item.notes}>
                      {item.notes}
                    </span>
                  ) : item.has_notes ? (
                    <span className="note-dot">● note</span>
                  ) : (
                    orDash(null)
                  )}
                </td>
              </tr>
            )}
          />
        )}
      </Card>

      <StatusTimeline resumeId={activeResumeId} />
    </div>
  )
}

/**
 * The immutable transition log.
 *
 * Separate from the table above because it answers a different question: the
 * table says where each application stands now, this says how it got there.
 */
function StatusTimeline({ resumeId }) {
  const history = useAsync(
    (signal) => listApplicationHistory({ resumeId, pageSize: 25 }, signal),
    [resumeId],
  )

  const items = history.data?.items ?? []

  return (
    <details className="group">
      <summary>
        <Icon name="clock" size={15} />
        Status change log
        {!history.loading && <Badge tone="neutral">{history.data?.pagination?.total ?? items.length}</Badge>}
      </summary>
      <div className="group-body">
        {history.loading ? (
          <Skeleton count={4} height={18} />
        ) : history.error ? (
          <ErrorState error={history.error} onRetry={history.run} />
        ) : items.length === 0 ? (
          <EmptyState icon="clock" title="No status changes recorded yet." />
        ) : (
          <ul className="timeline">
            {items.map((entry) => (
              <li key={entry.id} className="timeline-item">
                <div className="small">
                  <strong>{entry.job_title ?? 'Untitled role'}</strong>
                  {entry.company ? <span className="subtle"> · {entry.company}</span> : null}
                </div>
                <div className="xs subtle">
                  {entry.old_status ? `${STATUS_LABELS[entry.old_status] ?? entry.old_status} → ` : ''}
                  <strong>{STATUS_LABELS[entry.new_status] ?? entry.new_status}</strong>
                </div>
                <div className="timeline-time">{relativeTime(entry.changed_at)}</div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </details>
  )
}
