/**
 * The two rules governing a remembered Find Matches result set.
 *
 * Run with Node's built-in runner, which needs no dependency:
 *
 *     node --test frontend/src/state/
 *
 * These test the rules, not the rendering. The component wiring is verified by
 * building the app and exercising it against the real API; what is worth
 * pinning here is the logic that decides whether a stored result set may be
 * shown at all, because getting it wrong leaks one resume's search into
 * another's screen and would be easy to miss by eye.
 */

import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  APPLICATION_PAGE_SIZE,
  EMPTY_SESSION,
  MAX_APPLICATION_PAGES,
  applicationPageRecords,
  clearedSession,
  collectApplicationRecords,
  statusMapFrom,
  withPersistedStatuses,
  hasResultsFor,
  sessionFor,
  withCareerDecision,
  withError,
  withForm,
  withOutcome,
} from './searchSession.js'

const RESUME_A = 'resume-a'
const RESUME_B = 'resume-b'

const OUTCOME_A = { results: [{ job_id: 'j1' }], career_pages: [] }
const OUTCOME_B = { results: [{ job_id: 'j2' }], career_pages: [] }

function searched(resumeId, outcome) {
  return withOutcome(EMPTY_SESSION, { resumeId, outcome })
}

describe('a fresh session', () => {
  it('starts with nothing to show', () => {
    assert.equal(EMPTY_SESSION.outcome, null)
    assert.equal(EMPTY_SESSION.resumeId, null)
    assert.equal(hasResultsFor(EMPTY_SESSION, RESUME_A), false)
  })

  it('shows nothing even when a resume is active', () => {
    // A new application session must open Find Matches empty, whatever the
    // user searched for yesterday. Nothing was stored, so nothing returns.
    assert.equal(sessionFor(EMPTY_SESSION, RESUME_A).outcome, null)
  })
})

describe('remembering a result set', () => {
  it('keeps the outcome for the resume that produced it', () => {
    const session = searched(RESUME_A, OUTCOME_A)

    assert.equal(hasResultsFor(session, RESUME_A), true)
    assert.deepEqual(sessionFor(session, RESUME_A).outcome, OUTCOME_A)
  })

  it('replaces the previous result set rather than merging', () => {
    const first = searched(RESUME_A, OUTCOME_A)
    const second = withOutcome(first, { resumeId: RESUME_A, outcome: OUTCOME_B })

    assert.deepEqual(sessionFor(second, RESUME_A).outcome, OUTCOME_B)
  })

  it('clears a previous error when a search succeeds', () => {
    const failed = withError(EMPTY_SESSION, {
      resumeId: RESUME_A,
      error: new Error('boom'),
    })
    const ok = withOutcome(failed, { resumeId: RESUME_A, outcome: OUTCOME_A })

    assert.equal(sessionFor(ok, RESUME_A).error, null)
  })
})

describe('resume isolation', () => {
  it('never shows one resume’s results under another', () => {
    const session = searched(RESUME_A, OUTCOME_A)

    assert.equal(hasResultsFor(session, RESUME_B), false)
    assert.equal(sessionFor(session, RESUME_B).outcome, null)
  })

  it('shows nothing when no resume is active', () => {
    const session = searched(RESUME_A, OUTCOME_A)

    assert.equal(sessionFor(session, null).outcome, null)
    assert.equal(sessionFor(session, undefined).outcome, null)
  })

  it('does not resurrect the first resume’s results after switching back', () => {
    // Switching away and back is fine: the results were never deleted, and
    // they still belong to A. What matters is that B never saw them.
    const session = searched(RESUME_A, OUTCOME_A)

    assert.equal(hasResultsFor(session, RESUME_B), false)
    assert.equal(hasResultsFor(session, RESUME_A), true)
  })

  it('drops the stored results when the form moves to another resume', () => {
    // The one way a leak could happen without anyone reading a mismatched
    // session: relabelling the stored entry with a new resume id while
    // carrying the old outcome across.
    const session = searched(RESUME_A, OUTCOME_A)
    const switched = withForm(session, {
      resumeId: RESUME_B,
      selectedPortalIds: ['p1'],
    })

    assert.equal(switched.resumeId, RESUME_B)
    assert.equal(switched.outcome, null, 'A’s results must not become B’s')
    assert.equal(hasResultsFor(switched, RESUME_B), false)
  })

  it('keeps the results when the form changes for the same resume', () => {
    const session = searched(RESUME_A, OUTCOME_A)
    const retouched = withForm(session, { resumeId: RESUME_A, limit: 50 })

    assert.deepEqual(sessionFor(retouched, RESUME_A).outcome, OUTCOME_A)
    assert.equal(retouched.limit, 50)
  })
})

describe('the search form', () => {
  it('remembers the boards and the size', () => {
    const session = withForm(EMPTY_SESSION, {
      resumeId: RESUME_A,
      selectedPortalIds: ['p1', 'p2'],
      limit: 100,
    })

    assert.deepEqual(sessionFor(session, RESUME_A).selectedPortalIds, ['p1', 'p2'])
    assert.equal(sessionFor(session, RESUME_A).limit, 100)
  })

  it('leaves an unmentioned field alone', () => {
    const first = withForm(EMPTY_SESSION, {
      resumeId: RESUME_A,
      selectedPortalIds: ['p1'],
      limit: 50,
    })
    const second = withForm(first, { resumeId: RESUME_A, limit: 20 })

    assert.deepEqual(second.selectedPortalIds, ['p1'])
    assert.equal(second.limit, 20)
  })

  it('distinguishes "no selection yet" from "everything deselected"', () => {
    // The page pre-ticks every board only when nothing was ever chosen. If an
    // empty array read as "nothing yet", deselecting every board would silently
    // re-tick them all on the next visit.
    assert.equal(EMPTY_SESSION.selectedPortalIds, null)

    const emptied = withForm(EMPTY_SESSION, {
      resumeId: RESUME_A,
      selectedPortalIds: [],
    })
    assert.deepEqual(emptied.selectedPortalIds, [])
    assert.notEqual(emptied.selectedPortalIds, null)
  })
})

describe('a failed search', () => {
  it('records the error and shows no results', () => {
    const error = new Error('portal exploded')
    const session = withError(EMPTY_SESSION, { resumeId: RESUME_A, error })

    assert.equal(sessionFor(session, RESUME_A).error, error)
    assert.equal(sessionFor(session, RESUME_A).outcome, null)
  })

  it('does not leave the previous result set on screen under the error', () => {
    const session = withError(searched(RESUME_A, OUTCOME_A), {
      resumeId: RESUME_A,
      error: new Error('boom'),
    })

    assert.equal(hasResultsFor(session, RESUME_A), false)
  })

  it('keeps its error to the resume it belongs to', () => {
    const session = withError(EMPTY_SESSION, {
      resumeId: RESUME_A,
      error: new Error('boom'),
    })

    assert.equal(sessionFor(session, RESUME_B).error, null)
  })
})

describe('career page decisions', () => {
  it('remembers a page the user acted on', () => {
    const session = withCareerDecision(searched(RESUME_A, OUTCOME_A), {
      officialCareerUrl: 'https://careers.example.com/',
      company: 'Example',
      status: 'applied',
    })

    assert.deepEqual(sessionFor(session, RESUME_A).decidedCareerPages, {
      'https://careers.example.com/': { company: 'Example', status: 'applied' },
    })
  })

  it('forgets them when a new search replaces the cards they described', () => {
    const decided = withCareerDecision(searched(RESUME_A, OUTCOME_A), {
      officialCareerUrl: 'https://careers.example.com/',
      company: 'Example',
      status: 'skipped',
    })
    const rerun = withOutcome(decided, { resumeId: RESUME_A, outcome: OUTCOME_B })

    assert.deepEqual(rerun.decidedCareerPages, {})
  })

  it('does not carry them to another resume', () => {
    const session = withCareerDecision(searched(RESUME_A, OUTCOME_A), {
      officialCareerUrl: 'https://careers.example.com/',
      company: 'Example',
      status: 'applied',
    })

    assert.deepEqual(sessionFor(session, RESUME_B).decidedCareerPages, {})
  })
})

describe('clearing', () => {
  it('returns to the fresh state', () => {
    const cleared = clearedSession()

    assert.equal(cleared.outcome, null)
    assert.equal(cleared.resumeId, null)
    assert.deepEqual(cleared.decidedCareerPages, {})
  })
})

describe('nothing is persisted anywhere', () => {
  it('exposes no storage of any kind', async () => {
    // The session lifetime is enforced by holding the result set only in
    // memory. If this module ever reached for storage, results would survive a
    // restart and yesterday's search would reappear — so the absence is the
    // feature and is asserted rather than trusted to review.
    const { readFile } = await import('node:fs/promises')
    const source = await readFile(
      new URL('./searchSession.js', import.meta.url),
      'utf8',
    )
    const code = source.replace(/\/\*\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '')

    for (const banned of ['localStorage', 'sessionStorage', 'indexedDB', 'document.cookie', 'fetch(']) {
      assert.equal(code.includes(banned), false, `${banned} must not appear`)
    }
  })
})


describe('persisted Applied/Skipped survives leaving the page', () => {
  // The bug this covers, exactly as reported: mark a job Applied, go to
  // Application History, come back, and the row said Not applied again. The
  // decision was saved correctly the whole time - Application History proved
  // it - but the remembered result set still carried the status captured when
  // the search ran, and re-rendering the row re-seeded it from that snapshot.
  const outcome = () => ({
    data: {
      returned_count: 3,
      results: [
        { job_id: 'job-a', title: 'Financial Analyst', application_status: 'not_applied' },
        { job_id: 'job-b', title: 'FP&A Analyst', application_status: 'not_applied' },
        { job_id: 'job-c', title: 'Treasury Analyst', application_status: 'not_applied' },
      ],
    },
  })

  const resultsOf = (value) => (value.data ?? value).results

  it('shows Applied for a job whose application says Applied', () => {
    const merged = withPersistedStatuses(outcome(), { 'job-a': 'applied' })

    assert.equal(resultsOf(merged)[0].application_status, 'applied')
  })

  it('shows Skipped for a job whose application says Skipped', () => {
    const merged = withPersistedStatuses(outcome(), { 'job-b': 'skipped' })

    assert.equal(resultsOf(merged)[1].application_status, 'skipped')
  })

  it('leaves a job with no application record as Not applied', () => {
    const merged = withPersistedStatuses(outcome(), { 'job-a': 'applied' })

    assert.equal(resultsOf(merged)[2].application_status, 'not_applied')
  })

  it('restores several different statuses at once', () => {
    // The reported sequence: A applied, B skipped, C never touched.
    const merged = withPersistedStatuses(outcome(), {
      'job-a': 'applied',
      'job-b': 'skipped',
    })

    assert.deepEqual(
      resultsOf(merged).map((job) => job.application_status),
      ['applied', 'skipped', 'not_applied'],
    )
  })

  it('survives restoring the same remembered outcome repeatedly', () => {
    // Navigating away and back re-runs the overlay against the *same* stored
    // snapshot every time. It has to keep producing the persisted answer, not
    // decay back towards the snapshot's own stale value.
    const stored = outcome()
    const map = { 'job-a': 'applied', 'job-b': 'skipped' }

    for (let visit = 0; visit < 3; visit += 1) {
      const merged = withPersistedStatuses(stored, map)
      assert.deepEqual(
        resultsOf(merged).map((job) => job.application_status),
        ['applied', 'skipped', 'not_applied'],
      )
    }
  })

  it('does not mutate the stored outcome', () => {
    // The session keeps the search's own answer. Overwriting it in place would
    // make the overlay unrepeatable and would quietly rewrite history.
    const stored = outcome()
    withPersistedStatuses(stored, { 'job-a': 'applied' })

    assert.equal(resultsOf(stored)[0].application_status, 'not_applied')
  })

  it('keeps a status the search already knew about', () => {
    // A job applied to before this search ran arrives already correct, and
    // must stay that way when no record is found for it.
    const stored = {
      results: [{ job_id: 'job-a', application_status: 'applied' }],
    }

    assert.equal(resultsOf(withPersistedStatuses(stored, {}))[0].application_status, 'applied')
  })

  it('returns the outcome untouched when there is nothing to overlay', () => {
    const stored = outcome()

    assert.equal(withPersistedStatuses(stored, {}), stored)
    assert.equal(withPersistedStatuses(stored, null), stored)
    assert.equal(withPersistedStatuses(null, { 'job-a': 'applied' }), null)
  })

  it('accepts an outcome that is not wrapped in a data envelope', () => {
    const bare = { results: [{ job_id: 'job-a', application_status: 'not_applied' }] }
    const merged = withPersistedStatuses(bare, { 'job-a': 'applied' })

    assert.equal(merged.results[0].application_status, 'applied')
    assert.equal(merged.data, undefined, 'the shape it arrived in is the shape it keeps')
  })
})

describe('the status lookup is keyed on the application record job id', () => {
  it('maps job_id to status', () => {
    const map = statusMapFrom([
      { job_id: 'job-a', status: 'applied' },
      { job_id: 'job-b', status: 'skipped' },
    ])

    assert.deepEqual(map, { 'job-a': 'applied', 'job-b': 'skipped' })
  })

  it('carries every status the applications API can return', () => {
    // Not just applied/skipped: a job moved to Interview or Offer elsewhere
    // must show that here rather than reverting to the search's snapshot.
    const map = statusMapFrom([
      { job_id: 'job-a', status: 'interview' },
      { job_id: 'job-b', status: 'offer' },
      { job_id: 'job-c', status: 'rejected' },
    ])

    assert.deepEqual(map, { 'job-a': 'interview', 'job-b': 'offer', 'job-c': 'rejected' })
  })

  it('skips malformed records rather than guessing', () => {
    const map = statusMapFrom([
      { job_id: 'job-a', status: 'applied' },
      { job_id: 'job-b' },
      { status: 'skipped' },
      null,
      undefined,
    ])

    assert.deepEqual(map, { 'job-a': 'applied' })
  })

  it('is empty for no applications', () => {
    assert.deepEqual(statusMapFrom([]), {})
    assert.deepEqual(statusMapFrom(undefined), {})
  })
})


describe('reading the applications the overlay is built from', () => {
  // The shape `listApplications` resolves to. The API client unwraps the
  // `{success, data, message, errors}` envelope in one place, so a caller
  // receives `data` itself: `Page[ApplicationOut]`, which is
  // `{items, pagination}`. Written out here as the endpoint returns it,
  // because assuming the wrong shape is the whole of the bug this covers.
  const page = (items, total = items.length, pageNumber = 1) => ({
    items,
    pagination: {
      page: pageNumber,
      page_size: APPLICATION_PAGE_SIZE,
      total,
      total_pages: Math.ceil(total / APPLICATION_PAGE_SIZE),
      has_next: pageNumber * APPLICATION_PAGE_SIZE < total,
    },
  })

  const record = (jobId, status) => ({
    id: 'app-' + jobId,
    resume_id: RESUME_A,
    job_id: jobId,
    status,
    notes: null,
    has_notes: false,
  })

  it('finds the records where the endpoint actually puts them', () => {
    const { items, total } = applicationPageRecords(
      page([record('job-a', 'applied'), record('job-b', 'skipped')]),
    )

    assert.equal(total, 2)
    assert.deepEqual(
      items.map((item) => [item.job_id, item.status]),
      [
        ['job-a', 'applied'],
        ['job-b', 'skipped'],
      ],
    )
  })

  it('refuses the double-unwrapped shape instead of quietly finding nothing', () => {
    // The regression test for the failed first attempt. It read
    // `page.data.items`, unwrapping an envelope the client had already
    // unwrapped. Nothing threw and nothing logged: `items` came back
    // undefined, `?? []` made it empty, and every row said Not applied while
    // Application History showed the truth. An unrecognised shape now has to
    // be loud, because a silent empty result is indistinguishable from a
    // resume that has genuinely never applied to anything.
    assert.throws(
      () => applicationPageRecords({ data: page([record('job-a', 'applied')]) }),
      TypeError,
    )
  })

  it('treats no page at all as no records', () => {
    assert.deepEqual(applicationPageRecords(null), { items: [], total: 0 })
  })

  it('reads a resume whose history spans several pages', async () => {
    const first = Array.from({ length: 3 }, (unused, index) =>
      record('job-' + index, 'applied'),
    )
    const pages = [page(first, 4, 1), page([record('job-3', 'skipped')], 4, 2)]
    const asked = []

    const records = await collectApplicationRecords(async (pageNumber) => {
      asked.push(pageNumber)
      return pages[pageNumber - 1]
    })

    assert.deepEqual(asked, [1, 2])
    assert.equal(records.length, 4)
    assert.equal(records.at(-1).status, 'skipped')
  })

  it('stops as soon as the records are all in hand', async () => {
    const asked = []
    await collectApplicationRecords(async (pageNumber) => {
      asked.push(pageNumber)
      return page([record('job-a', 'applied')])
    })

    assert.deepEqual(asked, [1], 'a complete first page must end the walk')
  })

  it('stops on an empty page even when the total disagrees', async () => {
    // A backend claiming more records than it will serve must not spin here.
    const asked = []
    await collectApplicationRecords(async (pageNumber) => {
      asked.push(pageNumber)
      return page([], 9999, pageNumber)
    })

    assert.deepEqual(asked, [1])
  })

  it('never pages past its ceiling', async () => {
    let asked = 0
    const records = await collectApplicationRecords(async (pageNumber) => {
      asked += 1
      return page([record('job-' + pageNumber, 'applied')], 9999, pageNumber)
    })

    assert.equal(asked, MAX_APPLICATION_PAGES)
    assert.equal(records.length, MAX_APPLICATION_PAGES)
  })

  it('returns nothing for a resume that has applied to nothing', async () => {
    const records = await collectApplicationRecords(async () => page([], 0))

    assert.deepEqual(records, [])
    assert.deepEqual(statusMapFrom(records), {})
  })
})


describe('the reported workflow, end to end over the state layer', () => {
  // Find Matches -> mark Job A Applied -> mark Job B Skipped -> navigate to
  // Application History -> navigate to Resume Management -> navigate back.
  //
  // Every navigation unmounts the page: `App.jsx` mounts the screen under
  // `key={route}`. So coming back re-reads the applications and rebuilds the
  // displayed statuses from them. This runs that path over the real response
  // shape, which is the part the previous attempt got wrong.
  const database = new Map()

  const snapshot = () => ({
    data: {
      returned_count: 3,
      results: [
        { job_id: 'job-a', title: 'Financial Analyst', application_status: 'not_applied' },
        { job_id: 'job-b', title: 'FP&A Analyst', application_status: 'not_applied' },
        { job_id: 'job-c', title: 'Treasury Analyst', application_status: 'not_applied' },
      ],
    },
  })

  /** What `PATCH /applications/{job_id}` records. */
  const mark = (jobId, status) => database.set(jobId, status)

  /** What `GET /applications?resume_id=` returns, envelope already stripped. */
  const listApplications = async () => ({
    items: [...database].map(([jobId, status]) => ({
      id: 'app-' + jobId,
      resume_id: RESUME_A,
      job_id: jobId,
      status,
    })),
    pagination: { page: 1, page_size: APPLICATION_PAGE_SIZE, total: database.size },
  })

  /** Everything returning to Find Matches does before the rows render. */
  async function returnToFindMatches(remembered) {
    const records = await collectApplicationRecords(listApplications)
    return withPersistedStatuses(remembered, statusMapFrom(records))
  }

  const statusOf = (merged, jobId) =>
    (merged.data ?? merged).results.find((job) => job.job_id === jobId).application_status

  it('shows Applied and Skipped after leaving and coming back', async () => {
    database.clear()
    mark('job-a', 'applied')
    mark('job-b', 'skipped')

    const merged = await returnToFindMatches(snapshot())

    assert.equal(statusOf(merged, 'job-a'), 'applied')
    assert.equal(statusOf(merged, 'job-b'), 'skipped')
    assert.equal(statusOf(merged, 'job-c'), 'not_applied')
  })

  it('does not decay over repeated navigation', async () => {
    database.clear()
    mark('job-a', 'applied')
    mark('job-b', 'skipped')

    for (let visit = 1; visit <= 5; visit += 1) {
      // The remembered outcome never changes: it is the snapshot the search
      // produced. Every visit must therefore rebuild the answer from the
      // database rather than from the previous visit's result.
      const merged = await returnToFindMatches(snapshot())
      assert.equal(statusOf(merged, 'job-a'), 'applied', 'visit ' + visit)
      assert.equal(statusOf(merged, 'job-b'), 'skipped', 'visit ' + visit)
    }
  })

  it('keeps up as the user changes several jobs in succession', async () => {
    database.clear()

    mark('job-a', 'applied')
    assert.equal(statusOf(await returnToFindMatches(snapshot()), 'job-a'), 'applied')

    mark('job-b', 'skipped')
    let merged = await returnToFindMatches(snapshot())
    assert.equal(statusOf(merged, 'job-a'), 'applied')
    assert.equal(statusOf(merged, 'job-b'), 'skipped')

    // Changing one's mind is a status like any other: the database says
    // not_applied, so the row says Not applied.
    mark('job-a', 'not_applied')
    merged = await returnToFindMatches(snapshot())
    assert.equal(statusOf(merged, 'job-a'), 'not_applied')
    assert.equal(statusOf(merged, 'job-b'), 'skipped')
  })

  it('does not invent a record for a job the user never touched', async () => {
    database.clear()
    mark('job-a', 'applied')

    const merged = await returnToFindMatches(snapshot())

    assert.equal(statusOf(merged, 'job-c'), 'not_applied')
    assert.equal(database.has('job-c'), false, 'nothing may be written on restore')
  })

  it('leaves the job data itself untouched, overlaying only the status', async () => {
    database.clear()
    mark('job-a', 'applied')

    const remembered = snapshot()
    const merged = await returnToFindMatches(remembered)
    const before = remembered.data.results[0]
    const after = (merged.data ?? merged).results[0]

    assert.equal(after.title, before.title)
    assert.equal(after.job_id, before.job_id)
    assert.equal(merged.data.returned_count, remembered.data.returned_count)
    assert.deepEqual(
      { ...after, application_status: null },
      { ...before, application_status: null },
    )
    assert.equal(
      before.application_status,
      'not_applied',
      'the remembered snapshot is read, never rewritten',
    )
  })

  it('carries the career pages and the run summary through untouched', async () => {
    // Career pages are a separate feature with a separate decision endpoint.
    // The status overlay rebuilds `results` and nothing else, so the cards,
    // the counters and the board legs come back exactly as the search left
    // them.
    database.clear()
    mark('job-a', 'applied')

    const remembered = snapshot()
    remembered.data.career_pages = [
      { company_name: 'Contoso Freight', official_career_url: 'https://contoso.example/careers' },
    ]
    remembered.data.summary = { collected_count: 42, portals: [] }

    const merged = await returnToFindMatches(remembered)

    assert.deepEqual(merged.data.career_pages, remembered.data.career_pages)
    assert.deepEqual(merged.data.summary, remembered.data.summary)
    assert.equal(statusOf(merged, 'job-a'), 'applied')
  })

  it('lets the database win when a session restore lands after the statuses', async () => {
    // The ordering the brief calls out: the statuses resolve, and only then
    // does another restore of the remembered outcome happen. The overlay is
    // applied on read rather than stored, so the later restore is re-overlaid
    // instead of overwriting anything.
    database.clear()
    mark('job-a', 'applied')

    const map = statusMapFrom(await collectApplicationRecords(listApplications))
    const restoredAgain = withPersistedStatuses(snapshot(), map)

    assert.equal(statusOf(restoredAgain, 'job-a'), 'applied')
  })
})


describe('the page reads the applications through the tested path', () => {
  it('never unwraps the applications response by hand', async () => {
    // The merge functions were correct and fully tested while the bug was
    // live. What was wrong was the handful of lines feeding them, and those
    // lines sat in a component no test could reach. They have moved into this
    // module, and this keeps them here: a second hand-rolled reader in the
    // page is how the fault would come back.
    const { readFile } = await import('node:fs/promises')
    const source = await readFile(
      new URL('../pages/FindMatches.jsx', import.meta.url),
      'utf8',
    )

    const start = source.indexOf('function usePersistedStatuses')
    assert.ok(start > 0, 'the persisted-status hook must exist')
    const hook = source.slice(start, source.indexOf('function SearchOutcome', start))
    const code = hook.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '')

    assert.ok(
      code.includes('collectApplicationRecords('),
      'the hook must read applications through collectApplicationRecords',
    )
    assert.equal(
      /\.data\b/.test(code),
      false,
      'the hook must not touch `.data`: listApplications already returns it',
    )
  })
})
