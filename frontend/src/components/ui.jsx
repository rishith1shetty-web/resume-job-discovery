/**
 * Presentational primitives shared by every page.
 *
 * Loading, empty and error states live here so the whole product expresses
 * them identically instead of each page inventing its own. Nothing in this
 * file fetches, derives or interprets data — a component that only knows how
 * something looks cannot disagree with the backend about what it means.
 */

import { Icon } from './Icon.jsx'

/* --- Surfaces ------------------------------------------------------------ */

export function Card({ title, subtitle, actions, children, className = '', flush = false }) {
  return (
    <section className={`card ${className}`}>
      {(title || actions) && (
        <header className="card-head">
          <div style={{ minWidth: 0 }}>
            {title && <h2 className="card-title">{title}</h2>}
            {subtitle && <p className="card-subtitle">{subtitle}</p>}
          </div>
          {actions && <div className="card-actions">{actions}</div>}
        </header>
      )}
      <div className={flush ? 'card-body card-body-flush' : 'card-body'}>{children}</div>
    </section>
  )
}

export function PageHeader({ title, lede, actions }) {
  return (
    <div className="page-head">
      <div style={{ minWidth: 0 }}>
        <h1 className="page-title">{title}</h1>
        {lede && <p className="page-lede">{lede}</p>}
      </div>
      {actions && <div className="page-head-actions">{actions}</div>}
    </div>
  )
}

/* --- Controls ------------------------------------------------------------ */

export function Button({ children, variant = 'default', size, icon, className = '', ...props }) {
  const classes = ['btn', `btn-${variant}`, size ? `btn-${size}` : '', className]
    .filter(Boolean)
    .join(' ')

  return (
    <button className={classes} {...props}>
      {icon && <Icon name={icon} />}
      {children}
    </button>
  )
}

export function IconButton({ icon, label, className = '', ...props }) {
  return (
    <button
      className={`btn btn-icon ${className}`}
      title={label}
      aria-label={label}
      type="button"
      {...props}
    >
      <Icon name={icon} />
    </button>
  )
}

/**
 * Segmented control. Rendered as a radiogroup rather than a row of buttons so
 * arrow keys move between options the way a keyboard user expects.
 */
export function SegmentedControl({ options, value, onChange, label, disabled = false }) {
  return (
    <div className="segmented" role="radiogroup" aria-label={label}>
      {options.map((option) => {
        const active = option.value === value
        return (
          <button
            key={option.value}
            type="button"
            role="radio"
            aria-checked={active}
            disabled={disabled}
            className={`segmented-item ${active ? 'segmented-item-active' : ''}`}
            onClick={() => onChange(option.value)}
          >
            {option.label}
          </button>
        )
      })}
    </div>
  )
}

export function FilterPill({ active, count, children, ...props }) {
  return (
    <button
      type="button"
      className={`filter-pill ${active ? 'filter-pill-active' : ''}`}
      aria-pressed={active}
      {...props}
    >
      {children}
      {count !== undefined && <span className="filter-pill-count">{count}</span>}
    </button>
  )
}

/* --- Indicators ---------------------------------------------------------- */

export function Badge({ children, tone = 'neutral', title, dot = false }) {
  return (
    <span className={`badge badge-${tone} ${dot ? 'badge-dot' : ''}`} title={title}>
      {children}
    </span>
  )
}

/**
 * One headline number.
 *
 * `value` is rendered as given: a stat that cannot be computed passes `null`
 * and gets an em dash, because a zero the user might read as a real count is
 * worse than an obvious absence.
 */
export function StatCard({
  label,
  value,
  hint,
  icon,
  tone = '',
  compact = false,
  text = false,
  loading = false,
}) {
  return (
    <div className={`stat ${compact ? 'stat-compact' : ''}`}>
      <div className="stat-head">
        {icon && (
          <span className={`stat-icon ${tone ? `stat-icon-${tone}` : ''}`}>
            <Icon name={icon} />
          </span>
        )}
        <span className="stat-label">{label}</span>
      </div>
      <div className={text ? 'stat-value-text' : 'stat-value'}>
        {loading ? (
          <span
            className="skeleton"
            style={{ display: 'block', width: 62, height: text ? 20 : 30 }}
            aria-hidden="true"
          />
        ) : value === null || value === undefined ? (
          <span className="dash">—</span>
        ) : (
          value
        )}
      </div>
      {hint && <div className="stat-hint">{hint}</div>}
    </div>
  )
}

/** Circular percentage. Green above 90 to mark an exceptional match. */
export function ScoreRing({ value, size = 58 }) {
  const percent = Math.max(0, Math.min(100, Number(value) || 0))
  const radius = size / 2 - 4
  const circumference = 2 * Math.PI * radius

  return (
    <div
      className="score-ring"
      style={{ width: size, height: size }}
      role="img"
      aria-label={`Match score ${percent.toFixed(1)} percent`}
    >
      <svg width={size} height={size}>
        <circle
          className="score-ring-track"
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          strokeWidth="4.5"
        />
        <circle
          className={`score-ring-fill ${percent >= 90 ? 'score-ring-fill-high' : ''}`}
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          strokeWidth="4.5"
          strokeDasharray={circumference}
          strokeDashoffset={circumference * (1 - percent / 100)}
        />
      </svg>
      <span className="score-ring-value">
        {Math.round(percent)}
        <span className="score-ring-unit">%</span>
      </span>
    </div>
  )
}

/** Horizontal 0–100 bar. `tone` is derived unless given. */
export function Meter({ value, tone, showValue = true, label }) {
  const percent = Math.max(0, Math.min(100, Number(value) || 0))
  const resolved = tone ?? (percent >= 75 ? 'good' : percent >= 50 ? 'warn' : 'weak')

  return (
    <div className="meter" role="img" aria-label={`${label ?? 'Value'}: ${Math.round(percent)}%`}>
      <span className="meter-track">
        <span className={`meter-fill meter-fill-${resolved}`} style={{ width: `${percent}%` }} />
      </span>
      {showValue && <span className="meter-value">{Math.round(percent)}%</span>}
    </div>
  )
}

/** Percentage bar for a 0–1 confidence score. */
export function ConfidenceBar({ value }) {
  return <Meter value={(Number(value) || 0) * 100} label="Confidence" />
}

/* --- States -------------------------------------------------------------- */

export function Spinner({ label = 'Loading…' }) {
  return (
    <div className="state state-loading" role="status">
      <span className="spinner" aria-hidden="true" />
      <span>{label}</span>
    </div>
  )
}

/**
 * Loading placeholder shaped like the content it replaces.
 *
 * Reserving the real height keeps the page from jumping when data lands,
 * which is the layout-shift rule in practice rather than in principle.
 */
export function Skeleton({ variant = 'text', count = 1, height, style }) {
  return (
    <>
      {Array.from({ length: count }, (_, index) => (
        <div
          key={index}
          className={`skeleton skeleton-${variant}`}
          style={{ height, width: variant === 'text' ? `${72 + ((index * 13) % 26)}%` : undefined, ...style }}
          aria-hidden="true"
        />
      ))}
    </>
  )
}

export function SkeletonCards({ count = 4, height = 128 }) {
  return (
    <div className="grid grid-stats" aria-hidden="true">
      <Skeleton variant="card" count={count} height={height} />
    </div>
  )
}

export function EmptyState({ title, hint, icon = 'inbox', children }) {
  return (
    <div className="state state-empty">
      <span className="state-icon">
        <Icon name={icon} />
      </span>
      <p className="state-title">{title}</p>
      {hint && <p className="state-hint">{hint}</p>}
      {children}
    </div>
  )
}

export function Notice({ tone = 'info', icon, children }) {
  const fallback = { good: 'checkCircle', warn: 'warning', bad: 'warning', info: 'info' }[tone]
  return (
    <div className={`notice notice-${tone}`}>
      <Icon name={icon ?? fallback} />
      <div>{children}</div>
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
    <div className="state state-error" role="alert">
      <p className="state-title">
        {isNetwork
          ? 'Cannot reach the service'
          : `Request failed${error.status ? ` (HTTP ${error.status})` : ''}`}
      </p>
      <p className="state-hint">{error.message}</p>

      {isNetwork && (
        <ul className="state-checklist">
          <li>Confirm the Rising Path service is running on this machine.</li>
          <li>
            Check that <code>VITE_API_BASE_URL</code> points at the right host and port.
          </li>
          <li>If the browser console reports CORS, add this origin to the backend allow-list.</li>
        </ul>
      )}

      {details.length > 0 && (
        <ul className="error-list">
          {details.map((item, index) => (
            <li key={`${item.code}-${index}`}>
              {item.field && <code className="error-field">{item.field}</code>}{' '}
              <span>{item.message}</span>
              <span className="error-code">{item.code}</span>
            </li>
          ))}
        </ul>
      )}

      {onRetry && (
        <Button variant="default" size="sm" icon="refresh" onClick={onRetry}>
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
export function AsyncBlock({ loading, error, data, onRetry, isEmpty, empty, skeleton, children }) {
  if (loading) return skeleton ?? <Spinner />
  if (error) return <ErrorState error={error} onRetry={onRetry} />
  if (data == null) return null
  if (isEmpty?.(data)) return empty ?? <EmptyState title="Nothing to show yet." />
  return children
}

/* --- Data display -------------------------------------------------------- */

export function Field({ label, children, hint }) {
  return (
    <div className="field">
      <span className="field-label">{label}</span>
      <span className="field-value">{children}</span>
      {hint && <span className="field-hint">{hint}</span>}
    </div>
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

/* --- Formatting ---------------------------------------------------------- */

/** Values the API may legitimately omit render as a dash, never as "null". */
export function orDash(value) {
  if (value === null || value === undefined || value === '') return <span className="dash">—</span>
  return value
}

export function formatDateTime(value) {
  if (!value) return null
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function formatDate(value) {
  if (!value) return null
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}

/**
 * "3 days ago" for recent timestamps, an absolute date beyond a month.
 *
 * Relative time is easier to judge freshness by, but stops being useful once
 * it is long enough that the reader would rather know the actual date.
 */
export function relativeTime(value) {
  if (!value) return null
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value

  const seconds = Math.round((Date.now() - date.getTime()) / 1000)
  if (seconds < 60) return 'Just now'
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes} min ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours} hour${hours === 1 ? '' : 's'} ago`
  const days = Math.round(hours / 24)
  if (days < 31) return `${days} day${days === 1 ? '' : 's'} ago`
  return formatDate(value)
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

/** Extension label from the content type the API returned, never guessed. */
export const CONTENT_TYPE_LABEL = {
  'application/pdf': 'PDF',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'DOCX',
  'text/plain': 'TXT',
}

export function fileKind(contentType) {
  return CONTENT_TYPE_LABEL[contentType] ?? 'FILE'
}
