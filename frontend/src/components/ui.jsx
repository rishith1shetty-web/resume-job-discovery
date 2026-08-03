/**
 * Small presentational primitives shared by every section.
 *
 * Loading, error and empty states live here so all seven sections express them
 * the same way instead of each inventing its own.
 */

export function Card({ title, subtitle, actions, children, className = '' }) {
  return (
    <section className={`card ${className}`}>
      {(title || actions) && (
        <header className="card-head">
          <div>
            {title && <h2 className="card-title">{title}</h2>}
            {subtitle && <p className="card-subtitle">{subtitle}</p>}
          </div>
          {actions && <div className="card-actions">{actions}</div>}
        </header>
      )}
      <div className="card-body">{children}</div>
    </section>
  )
}

export function Button({ children, variant = 'default', ...props }) {
  return (
    <button className={`btn btn-${variant}`} {...props}>
      {children}
    </button>
  )
}

export function Badge({ children, tone = 'neutral', title }) {
  return (
    <span className={`badge badge-${tone}`} title={title}>
      {children}
    </span>
  )
}

export function Spinner({ label = 'Loading…' }) {
  return (
    <div className="state state-loading" role="status">
      <span className="spinner" aria-hidden="true" />
      <span>{label}</span>
    </div>
  )
}

export function EmptyState({ title, hint, children }) {
  return (
    <div className="state state-empty">
      <p className="state-title">{title}</p>
      {hint && <p className="state-hint">{hint}</p>}
      {children}
    </div>
  )
}

/**
 * Render an ApiError.
 *
 * The backend's `errors[]` carries a dotted `field` such as
 * `body.requested_limit`, so each entry is shown with its field rather than
 * collapsed into one opaque line.
 */
export function ErrorState({ error, onRetry }) {
  if (!error) return null

  const isNetwork = error.isNetworkError
  const details = error.errors ?? []

  return (
    <div className={`state state-error ${isNetwork ? 'state-error-network' : ''}`} role="alert">
      <p className="state-title">
        {isNetwork ? 'Backend unavailable' : `Request failed${error.status ? ` (HTTP ${error.status})` : ''}`}
      </p>
      <p className="state-hint">{error.message}</p>

      {isNetwork && (
        <ul className="state-checklist">
          <li>Start the API: <code>python -m uvicorn app.main:app --host 127.0.0.1 --port 8100</code></li>
          <li>Confirm <code>VITE_API_BASE_URL</code> points at the right host and port.</li>
          <li>
            If the browser console shows a CORS error, add this dashboard's origin to
            <code> CORS_ALLOW_ORIGINS</code> in the backend <code>.env</code>.
          </li>
        </ul>
      )}

      {details.length > 0 && (
        <ul className="error-list">
          {details.map((item, index) => (
            <li key={`${item.code}-${index}`}>
              {item.field && <code className="error-field">{item.field}</code>}
              <span>{item.message}</span>
              <span className="error-code">{item.code}</span>
            </li>
          ))}
        </ul>
      )}

      {onRetry && (
        <Button variant="ghost" onClick={onRetry}>
          Try again
        </Button>
      )}
    </div>
  )
}

/**
 * One wrapper for the three states every data view needs.
 * `isEmpty` is a function so it is only evaluated once data exists.
 */
export function AsyncBlock({ loading, error, data, onRetry, isEmpty, empty, children }) {
  if (loading) return <Spinner />
  if (error) return <ErrorState error={error} onRetry={onRetry} />
  if (data == null) return null
  if (isEmpty?.(data)) return empty ?? <EmptyState title="Nothing to show yet." />
  return children
}

export function Field({ label, children, hint }) {
  return (
    <div className="field">
      <span className="field-label">{label}</span>
      <span className="field-value">{children}</span>
      {hint && <span className="field-hint">{hint}</span>}
    </div>
  )
}

/** Percentage bar for a 0–1 confidence score. */
export function ConfidenceBar({ value }) {
  const percent = Math.round((Number(value) || 0) * 100)
  const tone = percent >= 75 ? 'good' : percent >= 50 ? 'warn' : 'weak'
  return (
    <span className="confidence">
      <span className="confidence-track">
        <span className={`confidence-fill confidence-${tone}`} style={{ width: `${percent}%` }} />
      </span>
      <span className="confidence-value">{percent}%</span>
    </span>
  )
}

export function Table({ columns, rows, renderRow, empty }) {
  if (!rows || rows.length === 0) {
    return empty ?? <EmptyState title="No records." />
  }
  return (
    <div className="table-scroll">
      <table className="table">
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column}>{column}</th>
            ))}
          </tr>
        </thead>
        <tbody>{rows.map(renderRow)}</tbody>
      </table>
    </div>
  )
}

/** Values the API may legitimately omit render as a dash, never as "null". */
export function orDash(value) {
  if (value === null || value === undefined || value === '') return <span className="dash">—</span>
  return value
}

export function formatDateTime(value) {
  if (!value) return null
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString()
}

export function formatBytes(bytes) {
  if (typeof bytes !== 'number') return null
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`
}

export function humanize(value) {
  if (!value) return value
  return String(value)
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (character) => character.toUpperCase())
}
