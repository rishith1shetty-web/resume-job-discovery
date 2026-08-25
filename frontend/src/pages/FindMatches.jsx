/**
 * Find Matches.
 *
 * Pick the boards, pick a size, search. Everything displayed afterwards comes
 * from the search response: there is no client-side scoring, filtering or
 * re-ranking, so what is on screen is exactly what the backend qualified.
 *
 * No portal is named anywhere in this file. The board list is whatever
 * `GET /portals` reports as selectable, which is what lets a board be added or
 * retired without touching the interface.
 *
 * The search runs synchronously on the backend and can take a couple of
 * minutes, so the button becomes a live progress state rather than a spinner
 * on an otherwise dead page — a user who cannot tell whether a long request is
 * still alive will reload and start it again.
 */

import { useEffect, useMemo, useRef, useState } from 'react'

import { decideCareerPage, listPortals, startSearchRun } from '../api/endpoints.js'
import { ApplicationStatusCell } from '../components/ApplicationStatus.jsx'
import { Icon } from '../components/Icon.jsx'
import { useAsync } from '../hooks/useAsync.js'
import { useActiveResume } from '../state/ActiveResume.jsx'
import { useSearchSession } from '../state/SearchSession.jsx'
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorState,
  Notice,
  PageHeader,
  ScoreRing,
  SegmentedControl,
  Skeleton,
  StatCard,
  Table,
  humanize,
  orDash,
  relativeTime,
} from '../components/ui.jsx'

const LIMITS = [
  { value: 20, label: 'Find 20' },
  { value: 50, label: 'Find 50' },
  { value: 100, label: 'Find 100' },
]

const MAX_PORTALS = 8

export function FindMatches({ navigate }) {
  const { activeResumeId, activeResume } = useActiveResume()
  const { sessionForResume, rememberOutcome, rememberError, rememberForm } =
    useSearchSession()
  const portals = useAsync((signal) => listPortals({}, signal), [])

  // The results, the chosen boards and the size live in the session store
  // rather than in this component, because this component is unmounted the
  // moment the user opens another tab. Reading through `sessionForResume`
  // means a result set produced by another resume is never visible here — the
  // isolation is a property of the read, not of a cleanup that might not have
  // run yet.
  const session = sessionForResume(activeResumeId)
  const outcome = session.outcome
  const error = session.error

  // `busy` stays local: an in-flight request belongs to this mount. If the
  // user navigates away mid-search the request is not cancelled and its result
  // is still stored, but a remount must not inherit a spinner it cannot own.
  const [busy, setBusy] = useState(false)

  const selected = useMemo(
    () => new Set(session.selectedPortalIds ?? []),
    [session.selectedPortalIds],
  )
  const limit = session.limit ?? 20

  const setSelected = (updater) => {
    const next = typeof updater === 'function' ? updater(selected) : updater
    rememberForm(activeResumeId, { selectedPortalIds: [...next] })
  }
  const setLimit = (value) => rememberForm(activeResumeId, { limit: value })

  const items = portals.data?.items ?? []
  const selectable = useMemo(() => items.filter((portal) => portal.is_selectable), [items])
  const unavailable = useMemo(() => items.filter((portal) => !portal.is_selectable), [items])

  // Every available board is pre-selected: the product's value is breadth, and
  // making the user tick five boxes before their first search is friction for
  // no gain. Deselecting is still one click.
  //
  // Only when this resume has no remembered selection though — `null` means
  // "never chosen", and an empty array means "deselected everything". Coming
  // back to the tab must show the boards the user actually chose rather than
  // silently re-ticking them all.
  useEffect(() => {
    if (selectable.length > 0 && session.selectedPortalIds == null) {
      rememberForm(activeResumeId, {
        selectedPortalIds: selectable.slice(0, MAX_PORTALS).map((portal) => portal.id),
      })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectable, session.selectedPortalIds, activeResumeId])

  const chosen = [...selected].filter((id) => selectable.some((portal) => portal.id === id))
  const tooMany = chosen.length > MAX_PORTALS

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
    // Pinned for the duration: the active resume could change while a long
    // search is running, and the answer belongs to the resume that asked.
    const searchedResumeId = activeResumeId
    setBusy(true)
    try {
      const result = await startSearchRun({
        resumeId: searchedResumeId,
        portalIds: chosen,
        limit,
      })
      rememberOutcome(searchedResumeId, result)
    } catch (searchError) {
      rememberError(searchedResumeId, searchError)
    } finally {
      setBusy(false)
    }
  }

  if (!activeResumeId) {
    return (
      <div className="stack">
        <PageHeader title="Find Matches" />
        <Card>
          <EmptyState
            icon="document"
            title="No active resume"
            hint="Scores and application history belong to one resume, so a search cannot run without one."
          >
            <Button variant="primary" icon="document" onClick={() => navigate('resumes')}>
              Choose a resume
            </Button>
          </EmptyState>
        </Card>
      </div>
    )
  }

  return (
    <div className="stack">
      <PageHeader
        title="Find Matches"
        lede="Every selected board is searched independently. Only genuine, US-based openings scoring 70% or higher are returned."
      />

      <Card
        title="Search"
        subtitle={`Matching against ${activeResume?.original_filename ?? 'the active resume'}.`}
      >
        {portals.loading ? (
          <Skeleton count={3} height={54} />
        ) : portals.error ? (
          <ErrorState error={portals.error} onRetry={portals.run} />
        ) : items.length === 0 ? (
          <EmptyState icon="layers" title="No job boards are configured." />
        ) : (
          <form onSubmit={search} className="stack">
            <div>
              <div className="row" style={{ marginBottom: 'var(--space-3)' }}>
                <span className="input-label">Job boards</span>
                <Badge tone="neutral">
                  {chosen.length} of {selectable.length} selected
                </Badge>
                <span className="spacer" />
                <button
                  type="button"
                  className="link-button"
                  onClick={() =>
                    setSelected(
                      chosen.length === selectable.length
                        ? new Set()
                        : new Set(selectable.slice(0, MAX_PORTALS).map((portal) => portal.id)),
                    )
                  }
                >
                  {chosen.length === selectable.length ? 'Clear all' : 'Select all'}
                </button>
              </div>

              <div className="tile-grid">
                {selectable.map((portal) => {
                  const isOn = selected.has(portal.id)
                  return (
                    <label key={portal.id} className={`tile ${isOn ? 'tile-selected' : ''}`}>
                      <input
                        type="checkbox"
                        checked={isOn}
                        onChange={() => toggle(portal.id)}
                        disabled={busy}
                      />
                      <span className="tile-check">
                        <Icon name="check" />
                      </span>
                      <span style={{ minWidth: 0 }}>
                        <span className="tile-name">{portal.display_name}</span>
                      </span>
                    </label>
                  )
                })}
              </div>
            </div>

            {unavailable.length > 0 && (
              <details className="group">
                <summary>
                  <Icon name="info" size={15} />
                  {unavailable.length} board(s) not available to search
                </summary>
                <div className="group-body">
                  <ul className="tight-list">
                    {unavailable.map((portal) => (
                      <li key={portal.id}>
                        <strong>{portal.display_name}</strong> —{' '}
                        {!portal.is_enabled
                          ? 'disabled'
                          : !portal.is_implemented
                            ? 'not yet integrated'
                            : 'unavailable'}
                        {portal.notes ? `. ${portal.notes}` : ''}
                      </li>
                    ))}
                  </ul>
                </div>
              </details>
            )}

            <div className="divider" />

            <div className="field-row">
              <div className="input-group" style={{ flex: '0 0 auto' }}>
                <span className="input-label">Result size</span>
                <SegmentedControl
                  options={LIMITS}
                  value={limit}
                  onChange={setLimit}
                  label="Number of matches to return"
                  disabled={busy}
                />
              </div>

              <span className="spacer" />

              <Button
                type="submit"
                variant="primary"
                size="lg"
                icon="search"
                disabled={busy || chosen.length === 0 || tooMany}
              >
                {busy ? `Searching ${chosen.length} board(s)…` : `Find ${limit} matches`}
              </Button>
            </div>

            {tooMany && <Notice tone="warn">Select at most {MAX_PORTALS} boards.</Notice>}
            {chosen.length === 0 && !tooMany && (
              <Notice tone="warn">Select at least one job board to search.</Notice>
            )}

            {busy && <SearchProgress count={chosen.length} />}
          </form>
        )}
      </Card>

      {error && <ErrorState error={error} onRetry={() => setError(null)} />}

      {outcome && <SearchOutcome outcome={outcome} resumeId={activeResumeId} />}
    </div>
  )
}

/**
 * Live feedback for a request that legitimately takes minutes.
 *
 * The elapsed counter is the point: an indeterminate bar alone looks identical
 * whether the search is working or has hung.
 */
function SearchProgress({ count }) {
  const [seconds, setSeconds] = useState(0)
  const startedAt = useRef(Date.now())

  useEffect(() => {
    startedAt.current = Date.now()
    const timer = setInterval(
      () => setSeconds(Math.round((Date.now() - startedAt.current) / 1000)),
      1000,
    )
    return () => clearInterval(timer)
  }, [])

  return (
    <div className="stack-sm" role="status" aria-live="polite">
      <div className="progress-rail" />
      <p className="muted small">
        Searching {count} board(s) — {seconds}s elapsed. Each board is searched to its own depth, so
        a full run can take a few minutes. Keep this page open until it finishes.
      </p>
    </div>
  )
}

/**
 * Companies with current US openings, and the official career page each one
 * publishes.
 *
 * Rendered as cards rather than as rows in the matches table, and deliberately
 * so: these are not vacancies. There is no score ring and no posted date,
 * because a career page has none of those things.
 *
 * What it does share with a job row is the decision the user makes about it.
 * Applied and Skipped are recorded per resume and both stop the page being
 * offered again by later searches — the same lifecycle a job has, for the same
 * reason: being shown a company you have already dealt with is the nuisance
 * this product exists to remove.
 *
 * Two things this does NOT mean, and the API enforces both:
 *   • applying to a career page is not applying to the jobs listed on it;
 *   • skipping one does not skip that company's individual jobs.
 *
 * The link is still an ordinary anchor. Nothing here applies to anything —
 * the user applies on the employer's own site and tells us what they did.
 *
 * Every URL rendered here has been fetched by the server: opened, redirects
 * followed, checked for a Company-ID or login wall, confirmed to belong to
 * this employer and confirmed to show job listings. A company whose page
 * failed any of that is absent from `career_pages` entirely rather than
 * present without a link, so this component never has to decide whether a URL
 * is safe to make clickable — an unverified one never arrives.
 */
function CareerPageCard({ page, resumeId, onDecided }) {
  const [pending, setPending] = useState(null)
  const [error, setError] = useState(null)

  async function decide(status) {
    // A second click while the first is in flight is ignored rather than sent.
    // The endpoint is idempotent either way, but not firing is cheaper than
    // relying on it.
    if (pending) return

    setPending(status)
    setError(null)
    try {
      await decideCareerPage(page, resumeId, { status })
      // Only after the server has confirmed. Removing the card first would
      // show the user a state the backend might have rejected.
      onDecided(page, status)
    } catch (err) {
      setError(err.message || 'Could not record that. Try again.')
      setPending(null)
    }
  }

  return (
    <article className="career-card">
      <div className="career-card-head">
        <h4 className="career-card-company">{page.company_name}</h4>
        <Badge tone="neutral">
          {page.opening_count} open{page.opening_count === 1 ? 'ing' : 'ings'}
        </Badge>
      </div>

      <p className="career-card-role">{page.relevant_role}</p>

      <p className="xs subtle row" style={{ gap: 'var(--space-1)' }}>
        <Icon name="pin" size={13} />
        {page.location}
      </p>

      {/* rel=noreferrer noopener on an external link the user opens
          themselves; target=_blank so the search they waited for is
          still here when they come back. */}
      <a
        className="btn btn-default career-card-link"
        href={page.official_career_url}
        target="_blank"
        rel="noreferrer noopener"
        title={
          page.validation_reason
            ? `${page.reason} ${page.validation_reason}`
            : page.reason
        }
      >
        View official careers
        <Icon name="external" size={14} />
      </a>

      <div className="career-card-actions">
        <Button
          size="sm"
          variant="primary"
          disabled={Boolean(pending)}
          onClick={() => decide('applied')}
          title="Record that you applied on this company's site. It will not be shown again for this resume."
        >
          {pending === 'applied' ? 'Saving…' : 'Applied'}
        </Button>
        <Button
          size="sm"
          variant="default"
          disabled={Boolean(pending)}
          onClick={() => decide('skipped')}
          title="Not interested. It will not be shown again for this resume."
        >
          {pending === 'skipped' ? 'Saving…' : 'Skip'}
        </Button>
      </div>

      {error ? <p className="xs bad">{error}</p> : null}

      <p className="xs subtle career-card-reason">{page.reason}</p>
      {/* Why the destination is trusted, in the server's own words. Shown
          rather than summarised, because "checked and found browsable" is the
          claim the user is relying on when they click. */}
      {page.validation_reason ? (
        <p className="xs subtle career-card-reason">{page.validation_reason}</p>
      ) : null}
    </article>
  )
}

function CareerPages({ pages, resumeId }) {
  // Cards the user has decided about, keyed the way the server keys them — by
  // career URL. Held in the session store rather than in this component for
  // the same reason the results are: the cached outcome still contains these
  // cards, because the server only stops returning them on the *next* search.
  // Kept here in component state instead, navigating away and back would
  // resurrect pages the user had already applied to or skipped.
  //
  // This is display bookkeeping only. The decision itself is already saved in
  // the database by the time it lands here.
  const { sessionForResume, rememberCareerDecision } = useSearchSession()
  const decided = sessionForResume(resumeId).decidedCareerPages

  const visible = pages.filter((page) => !(page.official_career_url in decided))

  function handleDecided(page, status) {
    rememberCareerDecision({
      officialCareerUrl: page.official_career_url,
      company: page.company_name,
      status,
    })
  }

  if (pages.length === 0) return null

  const recorded = Object.values(decided)

  return (
    <Card
      title={`Career pages (${visible.length})`}
      subtitle="Companies hiring for your role in the US right now. Every page below was opened and checked before it was listed. Apply on the company's own site, then mark it — it will not be offered again for this resume."
      actions={<Badge tone="accent">Verified pages only</Badge>}
    >
      {recorded.length > 0 ? (
        <p className="xs subtle" style={{ marginBottom: 'var(--space-3)' }}>
          Recorded this session:{' '}
          {recorded
            .map((entry) => `${entry.company} (${entry.status === 'applied' ? 'Applied' : 'Skipped'})`)
            .join(', ')}
          . They are in Application History.
        </p>
      ) : null}

      {visible.length === 0 ? (
        <p className="subtle">
          You have acted on every career page from this search. They are in
          Application History.
        </p>
      ) : (
        <div className="grid grid-3">
          {visible.map((page) => (
            <CareerPageCard
              key={page.official_career_url + page.company_name}
              page={page}
              resumeId={resumeId}
              onDecided={handleDecided}
            />
          ))}
        </div>
      )}
    </Card>
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
  const summary = run.summary ?? {}

  const collected = summary.collected_count ?? 0
  const nonUs = summary.non_us_excluded_count ?? 0
  const quickApply = summary.quick_apply_excluded_count ?? 0
  // Everything collected that survived the location gate. Derived rather than
  // read, because the backend reports what it excluded, not what it kept.
  const usJobs = Math.max(0, collected - nonUs)

  // Phase 12 records how deep each board went in the run summary. The leg rows
  // carry no column for it, so they are joined by slug here.
  const depthBySlug = Object.fromEntries((summary.portals ?? []).map((leg) => [leg.portal, leg]))

  return (
    <div className="stack">
      <section className="grid grid-stats">
        <StatCard label="Jobs collected" value={collected} icon="layers" compact hint="Across all boards" />
        <StatCard label="US jobs" value={usJobs} icon="pin" compact hint={`${nonUs} outside the US removed`} />
        <StatCard
          label="Quick Apply rejected"
          value={quickApply}
          icon="ban"
          tone="bad"
          compact
          hint="One-click listings are never shown"
        />
        <StatCard
          label="Qualified"
          value={summary.candidate_pool_size ?? 0}
          icon="checkCircle"
          tone="good"
          compact
          hint="Scored 70% or above"
        />
        <StatCard
          label="Returned"
          value={run.returned_count}
          icon="briefcase"
          tone="info"
          compact
          hint={`of ${run.requested_limit} requested`}
        />
      </section>

      {run.is_short && (
        <Notice tone="warn">
          <strong>
            {run.returned_count} of {run.requested_limit} requested jobs qualified.
          </strong>{' '}
          The 70% threshold is never lowered to fill the count — the remainder simply were not good
          enough matches.
        </Notice>
      )}

      {/* One block rather than a stack of identical blue boxes: three
          full-width notices pushed the results themselves below the fold,
          which is the wrong thing to make the user scroll past. */}
      <SearchNotes summary={summary} warnings={run.warnings ?? []} />

      <Card
        title={`Matches (${results.length})`}
        subtitle="Open a listing and apply on the employer's own site, then record what happened."
        actions={<Badge tone={RUN_TONES[run.status] ?? 'neutral'}>{humanize(run.status)}</Badge>}
        flush
      >
        {results.length === 0 ? (
          <EmptyState
            icon="search"
            title="No jobs qualified"
            hint="Every listing collected was either the wrong role, outside the US, a one-click application, or scored below 70% for this resume. The threshold is not lowered to produce results."
          />
        ) : (
          <div>
            {results.map((job) => (
              <JobRow key={job.job_id} job={job} resumeId={resumeId} />
            ))}
          </div>
        )}
      </Card>

      <CareerPages pages={run.career_pages ?? []} resumeId={resumeId} />

      <details className="group">
        <summary>
          <Icon name="layers" size={15} />
          Board breakdown
        </summary>
        <div className="group-body" style={{ padding: 0 }}>
          <Table
            columns={['Board', 'Status', 'Collected', 'Qualified', 'Returned', 'Depth reached']}
            rows={run.portals ?? []}
            renderRow={(leg) => {
              const depth = depthBySlug[leg.portal_slug]
              return (
                <tr key={leg.id}>
                  <td>
                    <span className="strong">{orDash(leg.portal_display_name)}</span>
                    {leg.error && <div className="xs" style={{ color: 'var(--danger)' }}>{leg.error}</div>}
                  </td>
                  <td>
                    <Badge
                      tone={
                        leg.status === 'completed' ? 'good' : leg.status === 'failed' ? 'bad' : 'neutral'
                      }
                    >
                      {humanize(leg.status)}
                    </Badge>
                  </td>
                  <td className="tabular">{leg.raw_count}</td>
                  <td className="tabular">{leg.qualified_count}</td>
                  <td className="tabular">{leg.selected_count}</td>
                  <td>
                    {depth ? (
                      <>
                        <Badge tone={depth.exhausted ? 'good' : 'warn'}>
                          {humanize(depth.exhaustion ?? 'unknown')}
                        </Badge>
                        <div className="xs subtle" style={{ marginTop: 3 }}>
                          {depth.requests} request(s) · {depth.pages} page(s)
                        </div>
                      </>
                    ) : (
                      orDash(null)
                    )}
                  </td>
                </tr>
              )
            }}
          />
        </div>
      </details>
    </div>
  )
}

/** How the returned set was arrived at, collapsed into one panel. */
function SearchNotes({ summary, warnings }) {
  const notes = [...warnings]
  if ((summary.portals_limited ?? []).length > 0) {
    notes.push(
      `Some boards stopped before running out of listings (${summary.portals_limited
        .map(humanize)
        .join(', ')}). More matches may exist than were collected.`,
    )
  }
  if (notes.length === 0) return null

  return (
    <Notice tone="info">
      {notes.length === 1 ? (
        notes[0]
      ) : (
        <ul className="tight-list">
          {notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      )}
    </Notice>
  )
}

function JobRow({ job, resumeId }) {
  return (
    <article className="job-card">
      <div className="job-main">
        <div className="job-title-row">
          <h3 className="job-title">{job.title}</h3>
          {job.is_new && <Badge tone="good">New</Badge>}
        </div>

        <p className="job-company">{job.company ?? 'Company not stated'}</p>

        <div className="job-facts">
          <span className="job-fact">
            <Icon name="pin" />
            {job.location ?? 'Location not stated'}
            {job.work_mode ? ` · ${humanize(job.work_mode)}` : ''}
          </span>
          <span className="job-fact">
            <Icon name="calendar" />
            {job.posted_at ? relativeTime(job.posted_at) : 'Posted date not stated'}
          </span>
          <span className="job-fact">
            <Icon name="layers" />
            {(job.portal_slugs ?? []).map(humanize).join(', ') || 'Unknown source'}
          </span>
        </div>

        {job.match_explanation && <p className="job-explain">{job.match_explanation}</p>}
      </div>

      <div className="job-side">
        <div className="job-side-actions">
          <ScoreRing value={job.final_score} />
          {/* A real anchor, not a button that calls window.open: middle-click,
              "open in new tab" and copy-link all have to work on the one
              control the whole product exists to deliver. */}
          {job.preferred_application_url ? (
            <a
              className="btn btn-primary btn-sm"
              href={job.preferred_application_url}
              target="_blank"
              rel="noreferrer noopener"
            >
              <Icon name="external" />
              Apply
            </a>
          ) : (
            <Badge tone="neutral">No link</Badge>
          )}
        </div>

        <ApplicationStatusCell
          jobId={job.job_id}
          resumeId={resumeId}
          initial={{ status: job.application_status }}
        />
      </div>
    </article>
  )
}
