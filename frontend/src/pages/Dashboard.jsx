/**
 * Executive dashboard — the first screen.
 *
 * Every number here is a total reported by the backend, read from the
 * `pagination.total` of a one-item request rather than by counting a page of
 * results client-side. Counting a page would silently under-report as soon as
 * there were more records than the page size, and a dashboard that quietly
 * understates the pipeline is worse than one that shows nothing.
 *
 * All figures are scoped to the active resume, because every score and every
 * application in this product belongs to one resume.
 */

import { ApiError } from '../api/client.js'
import { getProfile, listApplications, listSearchRuns } from '../api/endpoints.js'
import { BrandMark } from '../components/Brand.jsx'
import { Icon } from '../components/Icon.jsx'
import { StatusBadge } from '../components/ApplicationStatus.jsx'
import { useAsync } from '../hooks/useAsync.js'
import { useActiveResume } from '../state/ActiveResume.jsx'
import {
  Button,
  Card,
  EmptyState,
  ErrorState,
  Skeleton,
  StatCard,
  humanize,
  relativeTime,
} from '../components/ui.jsx'

/**
 * A count without its rows: page_size 1 and read the total.
 *
 * Resolves to null rather than throwing. Each figure on this dashboard is
 * independent, so one endpoint failing should cost that one tile — which then
 * shows an em dash — instead of blanking the whole row. (`GET /jobs` in
 * particular can fail on data this UI does not control: its response schema
 * declares `rejection_reasons` as a list of strings while the scorer stores
 * objects, so a page containing such a match returns 500. That is a backend
 * defect outside this phase's scope, but the dashboard still has to survive
 * it.)
 */
async function totalOf(loader, signal) {
  try {
    const response = await loader(signal)
    return response?.pagination?.total ?? (response?.items ?? []).length
  } catch {
    return null
  }
}

export function Dashboard({ navigate }) {
  // The provider resolves the stored id into a full record, so this screen
  // reads the resume rather than looking it up itself.
  const { activeResumeId, activeResume } = useActiveResume()
  const resolvedResume = activeResume

  /**
   * Matches returned by this resume's most recent completed search.
   *
   * Deliberately *not* `GET /jobs`: that endpoint outer-joins the match, so
   * it returns every job in the library — 1163 of them here — regardless of
   * which resume is asking. Presenting that as this resume's matches would
   * have been wrong by more than an order of magnitude. A search run's own
   * per-board `selected_count` is the number the backend actually stands
   * behind, and it belongs to one resume.
   */
  const latestSearch = useAsync(
    async (signal) => {
      if (!activeResumeId) return null
      try {
        const page = await listSearchRuns({ resumeId: activeResumeId, pageSize: 20 }, signal)
        const runs = (page?.items ?? []).filter(
          (run) => run.status === 'completed' || run.status === 'completed_with_errors',
        )
        if (runs.length === 0) return { count: null, at: null, total: page?.pagination?.total ?? 0 }
        const newest = runs.reduce((a, b) =>
          new Date(b.completed_at ?? b.created_at) > new Date(a.completed_at ?? a.created_at) ? b : a,
        )
        const returned = (newest.portals ?? []).reduce(
          (sum, leg) => sum + (leg.selected_count ?? 0),
          0,
        )
        return {
          count: returned,
          at: newest.completed_at ?? newest.created_at,
          total: page?.pagination?.total ?? runs.length,
        }
      } catch {
        return null
      }
    },
    [activeResumeId],
    { immediate: Boolean(activeResumeId) },
  )

  const stats = useAsync(
    async (signal) => {
      if (!activeResumeId) return null
      const [applied, interview, offer, rejected] = await Promise.all([
        totalOf(
          (s) => listApplications({ resumeId: activeResumeId, status: 'applied', pageSize: 1 }, s),
          signal,
        ),
        totalOf(
          (s) => listApplications({ resumeId: activeResumeId, status: 'interview', pageSize: 1 }, s),
          signal,
        ),
        totalOf(
          (s) => listApplications({ resumeId: activeResumeId, status: 'offer', pageSize: 1 }, s),
          signal,
        ),
        totalOf(
          (s) => listApplications({ resumeId: activeResumeId, status: 'rejected', pageSize: 1 }, s),
          signal,
        ),
      ])
      return { applied, interview, offer, rejected }
    },
    [activeResumeId],
    { immediate: Boolean(activeResumeId) },
  )

  const profile = useAsync(
    (signal) => (activeResumeId ? getProfile(activeResumeId, signal) : Promise.resolve(null)),
    [activeResumeId],
    { immediate: Boolean(activeResumeId) },
  )

  const recent = useAsync(
    (signal) =>
      activeResumeId
        ? listApplications({ resumeId: activeResumeId, pageSize: 6 }, signal)
        : Promise.resolve(null),
    [activeResumeId],
    { immediate: Boolean(activeResumeId) },
  )

  const notAnalysed = profile.error instanceof ApiError && profile.error.status === 404
  const targetRole = profile.data?.effective_target_role

  return (
    <div className="stack">
      <Welcome
        resume={resolvedResume}
        targetRole={targetRole}
        navigate={navigate}
        hasResume={Boolean(activeResumeId)}
      />

      {!activeResumeId ? (
        <Card>
          <EmptyState
            icon="document"
            title="No active resume yet"
            hint="Everything in this platform — matches, scores and application history — belongs to one resume. Upload one to begin."
          >
            <Button variant="primary" icon="upload" onClick={() => navigate('resumes')}>
              Upload a resume
            </Button>
          </EmptyState>
        </Card>
      ) : (
        <>
          <section className="grid grid-stats">
            <StatCard
              label="Active resume"
              value={resolvedResume?.original_filename ?? 'Selected'}
              hint={
                notAnalysed
                  ? 'Not analysed yet'
                  : targetRole
                    ? `Target: ${humanize(targetRole)}`
                    : 'Analysis pending'
              }
              icon="document"
              text
            />
            <StatCard
              label="Matches found"
              value={latestSearch.data?.count}
              loading={latestSearch.loading}
              hint={
                latestSearch.data?.at
                  ? `Latest search · ${relativeTime(latestSearch.data.at)}`
                  : 'No completed searches yet'
              }
              icon="briefcase"
            />
            <StatCard
              label="Applications"
              value={stats.data?.applied}
              loading={stats.loading}
              hint="Submitted by you"
              icon="send"
              tone="info"
            />
            <StatCard
              label="Interviews"
              value={stats.data?.interview}
              loading={stats.loading}
              hint="Currently in progress"
              icon="users"
              tone="good"
            />
            <StatCard
              label="Offers"
              value={stats.data?.offer}
              loading={stats.loading}
              hint="Received to date"
              icon="award"
              tone="good"
            />
          </section>

          {stats.error && <ErrorState error={stats.error} onRetry={stats.run} />}

          <div className="grid grid-2">
            <RecentActivity state={recent} navigate={navigate} />
            <PipelineCard stats={stats.data} loading={stats.loading} navigate={navigate} />
          </div>
        </>
      )}
    </div>
  )
}

function Welcome({ resume, targetRole, navigate, hasResume }) {
  const hour = new Date().getHours()
  const greeting = hour < 12 ? 'Good morning' : hour < 18 ? 'Good afternoon' : 'Good evening'

  return (
    <section className="hero">
      <div className="hero-copy">
        <span className="hero-eyebrow">
          <Icon name="sparkle" size={13} />
          Rising Path Recruiters
        </span>
        <h2 className="hero-title">{greeting}</h2>
        <p className="hero-sub">
          {hasResume ? (
            <>
              Working from <strong style={{ color: 'var(--brand-gold)' }}>
                {resume?.original_filename ?? 'the active resume'}
              </strong>
              {targetRole ? (
                <>
                  {' '}
                  against the role <strong style={{ color: 'var(--brand-gold)' }}>
                    {humanize(targetRole)}
                  </strong>.
                </>
              ) : (
                '. Run the analysis to detect its target role.'
              )}{' '}
              Every match is a real, US-based opening scoring 70% or higher.
            </>
          ) : (
            'Upload a resume and this platform will identify its target role, search every connected job board, and return only genuine US openings worth applying to.'
          )}
        </p>
        <div className="hero-actions">
          <Button variant="primary" icon="search" onClick={() => navigate('find')} disabled={!hasResume}>
            Find matches
          </Button>
          <Button
            variant="default"
            icon={hasResume ? 'insight' : 'upload'}
            onClick={() => navigate(hasResume ? 'analysis' : 'resumes')}
          >
            {hasResume ? 'View analysis' : 'Upload resume'}
          </Button>
        </div>
      </div>
      <BrandMark />
    </section>
  )
}

function PipelineCard({ stats, loading, navigate }) {
  const stages = [
    { key: 'applied', label: 'Applied', icon: 'send', tone: 'info' },
    { key: 'interview', label: 'Interview', icon: 'users', tone: 'good' },
    { key: 'offer', label: 'Offer', icon: 'award', tone: 'good' },
    { key: 'rejected', label: 'Rejected', icon: 'ban', tone: 'bad' },
  ]

  const peak = Math.max(1, ...stages.map((stage) => stats?.[stage.key] ?? 0))
  const nothingYet = !loading && stages.every((stage) => !stats?.[stage.key])

  return (
    <Card
      title="Application pipeline"
      subtitle="Outcomes you have recorded for this resume."
      actions={
        <Button variant="ghost" size="sm" onClick={() => navigate('history')}>
          Open history
        </Button>
      }
    >
      {loading ? (
        <Skeleton count={4} height={30} />
      ) : nothingYet ? (
        <EmptyState
          icon="flag"
          title="Nothing recorded yet"
          hint="Once you apply to a match and set its status, the pipeline appears here."
        />
      ) : (
        <div className="stack-sm">
          {stages.map((stage) => {
            const value = stats?.[stage.key] ?? 0
            return (
              <div key={stage.key} className="row">
                <span className={`stat-icon stat-icon-${stage.tone}`} style={{ width: 30, height: 30 }}>
                  <Icon name={stage.icon} size={15} />
                </span>
                <span className="strong small" style={{ minWidth: 76 }}>
                  {stage.label}
                </span>
                <span className="meter-track" style={{ flex: 1 }}>
                  <span
                    className={`meter-fill ${stage.key === 'rejected' ? 'meter-fill-weak' : ''}`}
                    style={{ width: `${(value / peak) * 100}%` }}
                  />
                </span>
                <span className="strong tabular" style={{ minWidth: 28, textAlign: 'right' }}>
                  {value}
                </span>
              </div>
            )
          })}
        </div>
      )}
    </Card>
  )
}

function RecentActivity({ state, navigate }) {
  const items = state.data?.items ?? []

  return (
    <Card
      title="Recent activity"
      subtitle="The last few applications you updated."
      actions={
        <Button variant="ghost" size="sm" icon="refresh" onClick={state.run} disabled={state.loading}>
          Refresh
        </Button>
      }
    >
      {state.loading ? (
        <Skeleton count={5} height={34} />
      ) : state.error ? (
        <ErrorState error={state.error} onRetry={state.run} />
      ) : items.length === 0 ? (
        <EmptyState
          icon="inbox"
          title="No applications recorded"
          hint="Find matches, apply on the employer's own site, then record the outcome here."
        >
          <Button variant="default" size="sm" icon="search" onClick={() => navigate('find')}>
            Find matches
          </Button>
        </EmptyState>
      ) : (
        <ul className="stack-sm">
          {items.map((item) => (
            <li key={item.id} className="row" style={{ alignItems: 'flex-start' }}>
              <span className="stat-icon" style={{ width: 32, height: 32 }}>
                <Icon name="briefcase" size={15} />
              </span>
              <span style={{ minWidth: 0, flex: 1 }}>
                <span className="strong small truncate" style={{ display: 'block' }}>
                  {item.job_title ?? 'Untitled role'}
                </span>
                <span className="xs subtle">
                  {item.company ?? 'Unknown company'} ·{' '}
                  {relativeTime(item.status_changed_at ?? item.updated_at)}
                </span>
              </span>
              <StatusBadge status={item.status} />
            </li>
          ))}
        </ul>
      )}
    </Card>
  )
}
