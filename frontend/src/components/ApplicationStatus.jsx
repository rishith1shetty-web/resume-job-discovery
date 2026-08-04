import { useState } from 'react'

import { getApplication, updateApplication } from '../api/endpoints.js'
import { Badge, Button, formatDateTime } from './ui.jsx'

/**
 * Manual application tracking for one canonical job under one resume.
 *
 * Records what the user says happened after they applied themselves. Nothing
 * here opens, fills or submits anything — the Apply link is a plain anchor and
 * this control only writes a status and a note.
 *
 * State is local to the row and updated from the PATCH response, so a change
 * shows immediately without a page refresh, polling or a socket.
 */

export const STATUSES = [
  'not_applied',
  'saved',
  'applied',
  'interview',
  'rejected',
  'offer',
  'skipped',
]

export const STATUS_LABELS = {
  not_applied: 'Not applied',
  saved: 'Saved',
  applied: 'Applied',
  interview: 'Interview',
  rejected: 'Rejected',
  offer: 'Offer',
  skipped: 'Skipped',
}

/**
 * Colour per status. `applied`, `interview`, `rejected` and `offer` are the
 * statuses that remove a job from future Find Matches runs for this resume, so
 * they are visually distinct from the ones that leave it eligible.
 */
export const STATUS_TONES = {
  not_applied: 'neutral',
  saved: 'accent',
  applied: 'good',
  interview: 'good',
  rejected: 'bad',
  offer: 'good',
  skipped: 'warn',
}

const EXCLUDING = new Set(['applied', 'interview', 'rejected', 'offer'])

export function StatusBadge({ status }) {
  return (
    <Badge
      tone={STATUS_TONES[status] ?? 'neutral'}
      title={
        EXCLUDING.has(status)
          ? 'Excluded from future searches for this resume.'
          : 'Still eligible in future searches.'
      }
    >
      {STATUS_LABELS[status] ?? status}
    </Badge>
  )
}

/**
 * The editable cell. `initial` seeds it from whatever the search returned, so
 * the row renders correctly before any request is made.
 */
export function ApplicationStatusCell({ jobId, resumeId, initial }) {
  const [status, setStatus] = useState(initial?.status ?? 'not_applied')
  const [notes, setNotes] = useState(initial?.notes ?? '')
  const [savedNotes, setSavedNotes] = useState(initial?.notes ?? '')
  const [updatedAt, setUpdatedAt] = useState(initial?.statusChangedAt ?? null)
  const [editing, setEditing] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  function applyRecord(record) {
    if (!record) return
    setStatus(record.status)
    setNotes(record.notes ?? '')
    setSavedNotes(record.notes ?? '')
    setUpdatedAt(record.status_changed_at ?? record.updated_at ?? null)
  }

  async function save(nextStatus, nextNotes) {
    setBusy(true)
    setError(null)
    try {
      const response = await updateApplication(jobId, resumeId, {
        status: nextStatus,
        notes: nextNotes,
      })
      applyRecord(response.data ?? response)
      setEditing(false)
    } catch (saveError) {
      setError(saveError)
      // Leave the previous value on screen: pretending the change landed
      // would be worse than showing that it did not.
    } finally {
      setBusy(false)
    }
  }

  async function refresh() {
    setBusy(true)
    setError(null)
    try {
      const detail = await getApplication(jobId, resumeId)
      if (detail.application) applyRecord(detail.application)
      else {
        setStatus('not_applied')
        setNotes('')
        setSavedNotes('')
        setUpdatedAt(null)
      }
    } catch (refreshError) {
      setError(refreshError)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="status-cell">
      <div className="status-controls">
        <select
          className="select select-compact"
          value={status}
          disabled={busy}
          aria-label="Application status"
          onChange={(event) => save(event.target.value, undefined)}
        >
          {STATUSES.map((option) => (
            <option key={option} value={option}>
              {STATUS_LABELS[option]}
            </option>
          ))}
        </select>

        <button
          type="button"
          className="icon-button"
          title="Refresh status"
          aria-label="Refresh status"
          disabled={busy}
          onClick={refresh}
        >
          ↻
        </button>
      </div>

      <div className="status-meta">
        <StatusBadge status={status} />
        {savedNotes.trim() && (
          <span className="note-dot" title={savedNotes}>
            ●&nbsp;note
          </span>
        )}
      </div>

      {updatedAt && (
        <div className="muted small">Updated {formatDateTime(updatedAt)}</div>
      )}

      {editing ? (
        <div className="note-editor">
          <textarea
            className="textarea"
            rows={3}
            value={notes}
            placeholder="Interview scheduled, waiting for HR, applied through referral…"
            onChange={(event) => setNotes(event.target.value)}
          />
          <div className="form-row">
            <Button
              type="button"
              disabled={busy}
              onClick={() => save(status, notes)}
            >
              Save note
            </Button>
            <Button
              type="button"
              variant="ghost"
              disabled={busy}
              onClick={() => {
                setNotes(savedNotes)
                setEditing(false)
              }}
            >
              Cancel
            </Button>
          </div>
        </div>
      ) : (
        <button
          type="button"
          className="link-button small"
          onClick={() => setEditing(true)}
        >
          {savedNotes.trim() ? 'Edit note' : 'Add note'}
        </button>
      )}

      {error && (
        <div className="muted small status-error">
          Could not save: {error.message ?? 'request failed'}
        </div>
      )}
    </div>
  )
}
