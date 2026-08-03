import {
  listApplicationHistory,
  listApplications,
  listJobs,
  listSearchRuns,
} from '../api/endpoints.js'
import { useAsync } from '../hooks/useAsync.js'
import { useActiveResume } from '../state/ActiveResume.jsx'
import {
  AsyncBlock,
  Badge,
  Button,
  Card,
  EmptyState,
  Table,
  formatDateTime,
  humanize,
  orDash,
} from '../components/ui.jsx'

const RUN_STATUS_TONE = {
  pending: 'neutral',
  running: 'warn',
  completed: 'good',
  completed_with_errors: 'warn',
  failed: 'bad',
  cancelled: 'neutral',
}

const APPLICATION_STATUS_TONE = {
  not_applied: 'neutral',
  saved: 'accent',
  applied: 'good',
  interview: 'good',
  rejected: 'bad',
  offer: 'good',
  skipped: 'neutral',
}

/**
 * Read-only views over the Phase 3 tables.
 *
 * Everything here is scoped to the active resume, because that is how the
 * backend stores it: results and application history never mix between
 * resumes.
 */
export function DataFoundation() {
  const { activeResumeId, activeResume } = useActiveResume()

  if (!activeResumeId) {
    return (
      <Card title="Data foundation">
        <EmptyState
          title="No resume selected."
          hint="These views are scoped to one resume. GET /jobs requires a resume_id."
        />
      </Card>
    )
  }

  return (
    <div className="stack">
      <p className="muted small">
        Scoped to <strong>{activeResume?.original_filename ?? activeResumeId}</strong>
      </p>
      <SearchRuns resumeId={activeResumeId} />
      <Jobs resumeId={activeResumeId} />
      <Applications resumeId={activeResumeId} />
      <History resumeId={activeResumeId} />
    </div>
  )
}

function SearchRuns({ resumeId }) {
  const runs = useAsync((signal) => listSearchRuns({ resumeId }, signal), [resumeId])
  const items = runs.data?.items ?? []

  return (
    <Card
      title="Search runs"
      subtitle="GET /search-runs"
      actions={
        <Button variant="ghost" onClick={runs.run} disabled={runs.loading}>
          Refresh
        </Button>
      }
    >
      <AsyncBlock
        loading={runs.loading}
        error={runs.error}
        data={runs.data}
        onRetry={runs.run}
        isEmpty={(data) => (data.items ?? []).length === 0}
        empty={
          <EmptyState
            title="No search runs for this resume."
            hint="Runs are created by POST /search-runs, which this dashboard deliberately does not call while the adapters are missing."
          />
        }
      >
        <Table
          columns={['Created', 'Limit', 'Min score', 'Status', 'Role searched', 'Portals']}
          rows={items}
          renderRow={(run) => (
            <tr key={run.id}>
              <td>
                {orDash(formatDateTime(run.created_at))}
                <div className="muted mono small">{run.id}</div>
              </td>
              <td>{run.requested_limit}</td>
              <td>{run.min_score}%</td>
              <td>
                <Badge tone={RUN_STATUS_TONE[run.status] ?? 'neutral'}>
                  {humanize(run.status)}
                </Badge>
                {run.error_message && (
                  <div className="muted small">{run.error_message}</div>
                )}
              </td>
              <td>
                {orDash(humanize(run.searched_target_role))}
                {run.searched_domain && (
                  <div className="muted small">{humanize(run.searched_domain)}</div>
                )}
              </td>
              <td>
                {/* Per-portal quotas and counters, the unit of failure
                    isolation: one portal can fail while the run succeeds. */}
                {(run.portals ?? []).length === 0
                  ? orDash(null)
                  : (run.portals ?? []).map((leg) => (
                      <div key={leg.id} className="portal-leg">
                        <Badge tone={leg.status === 'failed' ? 'bad' : 'neutral'}>
                          {humanize(leg.status)}
                        </Badge>
                        <span>{leg.portal_display_name ?? leg.portal_slug ?? leg.portal_id}</span>
                        <span className="muted small">
                          quota {leg.target_quota} · raw {leg.raw_count} · qualified{' '}
                          {leg.qualified_count} · selected {leg.selected_count}
                          {leg.shortfall > 0 && ` · short ${leg.shortfall}`}
                        </span>
                        {leg.error && <span className="muted small">{leg.error}</span>}
                      </div>
                    ))}
              </td>
            </tr>
          )}
        />
      </AsyncBlock>
    </Card>
  )
}

function Jobs({ resumeId }) {
  const jobs = useAsync((signal) => listJobs({ resumeId }, signal), [resumeId])
  const items = jobs.data?.items ?? []

  return (
    <Card
      title="Jobs"
      subtitle="GET /jobs — scored and status-tagged for this resume"
      actions={
        <Button variant="ghost" onClick={jobs.run} disabled={jobs.loading}>
          Refresh
        </Button>
      }
    >
      <AsyncBlock
        loading={jobs.loading}
        error={jobs.error}
        data={jobs.data}
        onRetry={jobs.run}
        isEmpty={(data) => (data.items ?? []).length === 0}
        empty={
          <EmptyState
            title="No jobs stored yet."
            hint="Jobs arrive once the portal adapters and the collection pipeline are built. Nothing is simulated here."
          />
        }
      >
        <Table
          columns={['Title', 'Company', 'Portals', 'Match', 'Posted', 'Status', 'Link']}
          rows={items}
          renderRow={(job) => (
            <tr key={job.id}>
              <td>
                <span className="strong">{job.title}</span>
                <div className="muted small">
                  {orDash(job.location)}
                  {job.work_mode && ` · ${humanize(job.work_mode)}`}
                </div>
              </td>
              <td>{orDash(job.company)}</td>
              <td>
                <div className="chip-row">
                  {(job.portals ?? []).map((slug) => (
                    <Badge key={slug} tone="neutral">
                      {slug}
                    </Badge>
                  ))}
                </div>
              </td>
              <td>
                {job.match ? (
                  <Badge tone={job.match.qualified ? 'good' : 'warn'}>
                    {Math.round(job.match.final_score)}%
                  </Badge>
                ) : (
                  <span className="muted small">Not scored</span>
                )}
              </td>
              <td>{orDash(formatDateTime(job.posted_at))}</td>
              <td>
                <Badge tone={APPLICATION_STATUS_TONE[job.application_status] ?? 'neutral'}>
                  {humanize(job.application_status)}
                </Badge>
                {/* Expiry is a job condition, never an application status. */}
                {job.is_expired && (
                  <div>
                    <Badge tone="bad">Expired</Badge>
                  </div>
                )}
              </td>
              <td>
                {job.application_url ? (
                  <a href={job.application_url} target="_blank" rel="noreferrer noopener">
                    Open
                  </a>
                ) : (
                  orDash(null)
                )}
              </td>
            </tr>
          )}
        />
      </AsyncBlock>
    </Card>
  )
}

function Applications({ resumeId }) {
  const applications = useAsync(
    (signal) => listApplications({ resumeId }, signal),
    [resumeId],
  )
  const items = applications.data?.items ?? []

  return (
    <Card
      title="Applications"
      subtitle="GET /applications — statuses the user sets by hand"
      actions={
        <Button variant="ghost" onClick={applications.run} disabled={applications.loading}>
          Refresh
        </Button>
      }
    >
      <AsyncBlock
        loading={applications.loading}
        error={applications.error}
        data={applications.data}
        onRetry={applications.run}
        isEmpty={(data) => (data.items ?? []).length === 0}
        empty={
          <EmptyState
            title="No applications tracked for this resume."
            hint="A record appears once a status is set for a job. Applications are always scoped to one resume."
          />
        }
      >
        <Table
          columns={['Job', 'Company', 'Status', 'Applied at', 'Updated', 'Notes']}
          rows={items}
          renderRow={(application) => (
            <tr key={application.id}>
              <td>
                {orDash(application.job_title)}
                <div className="muted mono small">{application.job_id}</div>
              </td>
              <td>{orDash(application.company)}</td>
              <td>
                <Badge tone={APPLICATION_STATUS_TONE[application.status] ?? 'neutral'}>
                  {humanize(application.status)}
                </Badge>
              </td>
              <td>{orDash(formatDateTime(application.applied_at))}</td>
              <td>{orDash(formatDateTime(application.updated_at))}</td>
              <td className="muted small">{orDash(application.notes)}</td>
            </tr>
          )}
        />
      </AsyncBlock>
    </Card>
  )
}

function History({ resumeId }) {
  const history = useAsync(
    (signal) => listApplicationHistory({ resumeId }, signal),
    [resumeId],
  )
  const items = history.data?.items ?? []

  return (
    <Card
      title="Application status history"
      subtitle="GET /application-history — immutable, newest first"
      actions={
        <Button variant="ghost" onClick={history.run} disabled={history.loading}>
          Refresh
        </Button>
      }
    >
      <AsyncBlock
        loading={history.loading}
        error={history.error}
        data={history.data}
        onRetry={history.run}
        isEmpty={(data) => (data.items ?? []).length === 0}
        empty={
          <EmptyState
            title="No status changes recorded."
            hint="Every transition writes one immutable record. A repeated identical update writes nothing."
          />
        }
      >
        <Table
          columns={['Changed at', 'Job', 'From', 'To', 'By', 'Note']}
          rows={items}
          renderRow={(entry) => (
            <tr key={entry.id}>
              <td>{orDash(formatDateTime(entry.changed_at))}</td>
              <td>{orDash(entry.job_title)}</td>
              <td>
                {entry.old_status ? (
                  <Badge tone="neutral">{humanize(entry.old_status)}</Badge>
                ) : (
                  <span className="muted small">created</span>
                )}
              </td>
              <td>
                <Badge tone={APPLICATION_STATUS_TONE[entry.new_status] ?? 'neutral'}>
                  {humanize(entry.new_status)}
                </Badge>
              </td>
              <td className="muted small">{orDash(entry.changed_by)}</td>
              <td className="muted small">{orDash(entry.note)}</td>
            </tr>
          )}
        />
      </AsyncBlock>
    </Card>
  )
}
