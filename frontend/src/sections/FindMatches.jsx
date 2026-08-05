import { useMemo, useState } from 'react'

import { listPortals, startSearchRun } from '../api/endpoints.js'
import { ApplicationStatusCell } from '../components/ApplicationStatus.jsx'
import { useAsync } from '../hooks/useAsync.js'
import {
  AsyncBlock,
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorState,
  Table,
  formatDateTime,
  humanize,
  orDash,
} from '../components/ui.jsx'
import { useActiveResume } from '../state/ActiveResume.jsx'

const LIMITS = [20, 50, 100]

/**
 * Find Matches: pick portals, pick a count, search.
 *
 * Everything shown comes from the search response. There is no mock data and
 * no client-side scoring — if a job is listed here the backend scored it at 70
 * or above for the active resume.
 *
 * The search runs synchronously on the backend, so the request stays open for
 * its duration. No portal is named in this file: the checkbox list is whatever
 * `GET /portals` reports as selectable.
 */
export function FindMatches() {
  const { activeResumeId } = useActiveResume()
  const portals = useAsync((signal) => listPortals({}, signal), [])

  const [selected, setSelected] = useState(() => new Set())
  const [limit, setLimit] = useState(20)
  const [busy, setBusy] = useState(false)
  const [outcome, setOutcome] = useState(null)
  const [error, setError] = useState(null)

  const items = portals.data?.items ?? []
  const selectable = useMemo(
    () => items.filter((portal) => portal.is_selectable),
    [items],
  )
  const unavailable = useMemo(
    () => items.filter((portal) => !portal.is_selectable),
    [items],
  )

  const chosen = [...selected].filter((id) =>
    selectable.some((portal) => portal.id === id),
  )
  const tooMany = chosen.length > 8

  function toggle(portalId) {
    setSelected((previous) => {
      const next = new Set(previous)
      if (next.has(portalId)) next.delete(portalId)
      else next.add(portalId)
      return next
    })
  }

  async function search(event) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    setOutcome(null)
    try {
      setOutcome(
        await startSearchRun({
          resumeId: activeResumeId,
          portalIds: chosen,
          limit,
        }),
      )
    } catch (searchError) {
      setError(searchError)
    } finally {
      setBusy(false)
    }
  }

  if (!activeResumeId) {
    return (
      <Card title="Find matches">
        <EmptyState
          title="Select an active resume first."
          hint="Scores and application history belong to one resume, so a search cannot run without one. Pick a resume under Resume management."
        />
      </Card>
    )
  }

  return (
    <div className="stack">
      <Card
        title="Find matches"
        subtitle="Searches every selected portal, then keeps only the jobs that score 70% or higher for the active resume."
      >
        <AsyncBlock
          loading={portals.loading}
          error={portals.error}
          data={portals.data}
          onRetry={portals.run}
          isEmpty={(data) => (data.items ?? []).length === 0}
          empty={<EmptyState title="No portals are registered yet." />}
        >
          <form onSubmit={search} className="stack">
            <div className="portal-picker">
              {selectable.map((portal) => (
                <label key={portal.id} className="portal-check">
                  <input
                    type="checkbox"
                    checked={selected.has(portal.id)}
                    onChange={() => toggle(portal.id)}
                  />
                  <span className="strong">{portal.display_name}</span>
                  <span className="muted small">{portal.slug}</span>
                </label>
              ))}
            </div>

            {unavailable.length > 0 && (
              <div className="notice notice-warn">
                <strong>Not available to search:</strong>
                <ul className="plain-list small">
                  {unavailable.map((portal) => (
                    <li key={portal.id}>
                      {portal.display_name} —{' '}
                      {!portal.is_enabled
                        ? 'disabled'
                        : !portal.is_implemented
                          ? 'no adapter yet'
                          : 'unavailable'}
                      {portal.notes ? `. ${portal.notes}` : ''}
                    </li>
                  ))}
                </ul>
              </div>
            )}

            <div className="search-controls">
              {LIMITS.map((option) => (
                <Button
                  key={option}
                  type="button"
                  variant={option === limit ? 'default' : 'ghost'}
                  onClick={() => setLimit(option)}
                  aria-pressed={option === limit}
                >
                  Find {option}
                </Button>
              ))}
              <span className="locked-threshold">
                Minimum match: <strong>70%</strong> <Badge tone="neutral">locked</Badge>
              </span>
            </div>

            {tooMany && (
              <p className="notice notice-warn">
                Select at most 8 portals.
              </p>
            )}

            <div className="form-row">
              <Button type="submit" disabled={busy || chosen.length === 0 || tooMany}>
                {busy ? `Searching ${chosen.length} portal(s)…` : 'Find matches'}
              </Button>
              <span className="muted small">
                {chosen.length === 0
                  ? 'Select at least one portal.'
                  : `${chosen.length} portal(s) selected. Runs synchronously — this can take a while.`}
              </span>
            </div>
          </form>
        </AsyncBlock>
      </Card>

      {error && <ErrorState error={error} onRetry={() => setError(null)} />}

      {outcome && <SearchOutcome outcome={outcome} resumeId={activeResumeId} />}
    </div>
  )
}

const RUN_TONES = {
  completed: 'good',
  completed_with_errors: 'warn',
  failed: 'bad',
  running: 'neutral',
  pending: 'neutral',
  cancelled: 'neutral',
}

function SearchOutcome({ outcome, resumeId }) {
  const run = outcome.data ?? outcome
  const results = run.results ?? []

  return (
    <div className="stack">
      <Card
        title="Search summary"
        actions={
          <Badge tone={RUN_TONES[run.status] ?? 'neutral'}>{humanize(run.status)}</Badge>
        }
      >
        <dl className="facts">
          <div>
            <dt>Requested</dt>
            <dd>{run.requested_limit}</dd>
          </div>
          <div>
            <dt>Returned</dt>
            <dd>{run.returned_count}</dd>
          </div>
          <div>
            <dt>Target role</dt>
            <dd>{orDash(run.target_role)}</dd>
          </div>
          <div>
            <dt>Threshold</dt>
            <dd>{run.min_score}%</dd>
          </div>
          {/* Read-only statement of a fixed policy, not a control: this
              product searches United States vacancies only. */}
          <div>
            <dt>Locations</dt>
            <dd>United States only</dd>
          </div>
        </dl>

        {run.is_short && (
          <p className="notice notice-warn">
            {run.returned_count} of {run.requested_limit} requested jobs qualified.
            The 70% threshold is never lowered to fill the count.
          </p>
        )}

        {(run.warnings ?? []).map((warning) => (
          <p key={warning} className="notice notice-warn small">
            {warning}
          </p>
        ))}

        <Table
          columns={['Portal', 'Status', 'Quota', 'Collected', 'Qualified', 'Selected', 'Window']}
          rows={run.portals ?? []}
          renderRow={(leg) => (
            <tr key={leg.id}>
              <td>
                <span className="strong">{orDash(leg.portal_display_name)}</span>
                {leg.error && <div className="muted small">{leg.error}</div>}
              </td>
              <td>
                <Badge
                  tone={
                    leg.status === 'completed'
                      ? 'good'
                      : leg.status === 'failed'
                        ? 'bad'
                        : 'neutral'
                  }
                >
                  {humanize(leg.status)}
                </Badge>
              </td>
              <td>{leg.target_quota}</td>
              <td>{leg.raw_count}</td>
              <td>{leg.qualified_count}</td>
              <td>{leg.selected_count}</td>
              <td className="small">{orDash(leg.deepest_window && humanize(leg.deepest_window))}</td>
            </tr>
          )}
        />

        {(run.queries ?? []).length > 0 && (
          <details className="small">
            <summary>Queries searched ({run.queries.length})</summary>
            <ul className="plain-list">
              {run.queries.map((query, index) => (
                <li key={`${query.portal}-${query.query_text}-${index}`}>
                  <span className="mono">{query.query_text}</span>{' '}
                  <span className="muted">
                    — {query.portal}, {humanize(query.window)}, {query.result_count} result(s)
                  </span>
                </li>
              ))}
            </ul>
          </details>
        )}
      </Card>

      <Card
        title={`Qualified jobs (${results.length})`}
        subtitle="Open a link and apply yourself. Nothing here submits an application."
      >
        {results.length === 0 ? (
          <EmptyState
            title="No jobs qualified."
            hint="Every collected listing was either the wrong role or scored below 70% for this resume. The threshold is not lowered to produce results."
          />
        ) : (
          <Table
            columns={[
              'Job',
              'Company',
              'Location',
              'Source',
              'Posted',
              'Match',
              'Status',
              'Apply',
            ]}
            rows={results}
            renderRow={(job) => (
              <tr key={job.job_id}>
                <td>
                  <span className="strong">{job.title}</span>
                  {job.is_new && (
                    <>
                      {' '}
                      <Badge tone="good">New</Badge>
                    </>
                  )}
                  {job.match_explanation && (
                    <div className="muted small">{job.match_explanation}</div>
                  )}
                </td>
                <td>{orDash(job.company)}</td>
                <td className="small">
                  {orDash(job.location)}
                  {job.work_mode && <div className="muted">{humanize(job.work_mode)}</div>}
                </td>
                <td className="small">
                  <div className="chip-row">
                    {(job.portal_slugs ?? []).map((slug) => (
                      <Badge key={slug} tone="neutral">
                        {slug}
                      </Badge>
                    ))}
                  </div>
                </td>
                <td className="small">{formatDateTime(job.posted_at)}</td>
                <td>
                  <span className="strong">{job.final_score.toFixed(1)}%</span>
                  <div className="muted small">{humanize(job.role_verdict)}</div>
                </td>
                <td>
                  <ApplicationStatusCell
                    jobId={job.job_id}
                    resumeId={resumeId}
                    initial={{ status: job.application_status }}
                  />
                </td>
                <td>
                  {job.preferred_application_url ? (
                    <a
                      href={job.preferred_application_url}
                      target="_blank"
                      rel="noreferrer noopener"
                    >
                      Open
                    </a>
                  ) : (
                    orDash(null)
                  )}
                </td>
              </tr>
            )}
          />
        )}

        <p className="muted small">
          Open a job, apply on the employer&apos;s own site, then record what
          happened here. Marking a job <strong>Applied</strong>,{' '}
          <strong>Interview</strong>, <strong>Rejected</strong> or{' '}
          <strong>Offer</strong> removes it from future searches for this
          resume; <strong>Saved</strong> and <strong>Skipped</strong> leave it
          eligible. Other resumes are unaffected.
        </p>
      </Card>
    </div>
  )
}
