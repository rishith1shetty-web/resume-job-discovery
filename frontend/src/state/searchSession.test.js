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
  EMPTY_SESSION,
  clearedSession,
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
