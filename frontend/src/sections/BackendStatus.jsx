import { getConfig, getHealth } from '../api/endpoints.js'
import { BASE_URL } from '../api/client.js'
import { useAsync } from '../hooks/useAsync.js'
import {
  AsyncBlock,
  Badge,
  Button,
  Card,
  ErrorState,
  Field,
  Spinner,
  formatDateTime,
  humanize,
  orDash,
} from '../components/ui.jsx'

const DEPENDENCY_TONE = {
  up: 'good',
  down: 'bad',
  degraded: 'warn',
  not_configured: 'neutral',
}

export function BackendStatus() {
  const health = useAsync((signal) => getHealth(signal), [])
  const config = useAsync((signal) => getConfig(signal), [])

  return (
    <div className="stack">
      <Card
        title="Backend status"
        subtitle={BASE_URL}
        actions={
          <Button variant="ghost" onClick={health.run} disabled={health.loading}>
            Refresh
          </Button>
        }
      >
        {health.loading && <Spinner label="Contacting the API…" />}

        {/* A transport failure is the only case with no payload at all. */}
        {!health.loading && health.error && (
          <ErrorState error={health.error} onRetry={health.run} />
        )}

        {!health.loading && !health.error && health.data && (
          <HealthDetail result={health.data} />
        )}
      </Card>

      <Card title="Client configuration" subtitle="Constants served by GET /config">
        <AsyncBlock
          loading={config.loading}
          error={config.error}
          data={config.data}
          onRetry={config.run}
        >
          {config.data && <ConfigDetail config={config.data} />}
        </AsyncBlock>
      </Card>
    </div>
  )
}

function HealthDetail({ result }) {
  const { healthy, httpStatus, payload, message } = result
  const checks = payload?.checks ?? []

  return (
    <div className="stack">
      <div className="status-row">
        <Badge tone={healthy ? 'good' : 'bad'}>
          {healthy ? 'API connected' : 'API reporting a problem'}
        </Badge>
        <span className="muted">HTTP {httpStatus}</span>
      </div>

      {/* 503 with a body is a real state: the API is up, a dependency is not. */}
      {!healthy && (
        <p className="notice notice-warn">
          {message || 'The API responded but reported itself unhealthy. Details below.'}
        </p>
      )}

      {payload && (
        <div className="field-grid">
          <Field label="Application">{orDash(payload.app_name)}</Field>
          <Field label="Version">{orDash(payload.version)}</Field>
          <Field label="Environment">{orDash(payload.environment)}</Field>
          <Field label="API version">{orDash(payload.api_version)}</Field>
          <Field label="Status">{orDash(humanize(payload.status))}</Field>
          <Field label="Uptime">
            {payload.uptime_seconds != null
              ? `${Math.round(payload.uptime_seconds)}s`
              : orDash(null)}
          </Field>
          <Field label="Checked at">{orDash(formatDateTime(payload.timestamp))}</Field>
        </div>
      )}

      {checks.length > 0 && (
        <div>
          <h3 className="section-heading">Dependencies</h3>
          <ul className="check-list">
            {checks.map((check) => (
              <li key={check.name} className="check-item">
                <div className="check-main">
                  <Badge tone={DEPENDENCY_TONE[check.status] ?? 'neutral'}>
                    {humanize(check.status)}
                  </Badge>
                  <strong>{humanize(check.name)}</strong>
                </div>
                <div className="check-meta">
                  {check.latency_ms != null && (
                    <span className="muted">{check.latency_ms} ms</span>
                  )}
                  {check.detail && <span className="muted">{check.detail}</span>}
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}

function ConfigDetail({ config }) {
  const search = config.search ?? {}
  const resume = config.resume ?? {}
  const application = config.application ?? {}

  return (
    <div className="stack">
      <div className="field-grid">
        <Field label="Result limits">{(search.result_limit_options ?? []).join(' · ')}</Field>
        <Field
          label="Minimum match"
          hint={search.min_match_score_locked ? 'Locked by the backend' : undefined}
        >
          {orDash(search.min_match_score)}
          {search.min_match_score != null && '%'}
        </Field>
        <Field label="Max enabled portals">{orDash(search.max_enabled_portals)}</Field>
        <Field label="Default sort">{orDash(humanize(search.default_sort))}</Field>
        <Field label="Upload limit">
          {resume.max_upload_mb != null ? `${resume.max_upload_mb} MB` : orDash(null)}
        </Field>
        <Field label="Accepted files">{(resume.supported_extensions ?? []).join(' ')}</Field>
      </div>

      <div>
        <h3 className="section-heading">Application statuses</h3>
        <div className="chip-row">
          {(application.statuses ?? []).map((status) => (
            <Badge key={status.value} tone="neutral" title={status.value}>
              {status.label}
            </Badge>
          ))}
        </div>
      </div>

      <div>
        <h3 className="section-heading">Freshness windows</h3>
        <div className="chip-row">
          {(search.freshness_windows ?? []).map((window) => (
            <Badge key={window.value} tone="neutral" title={window.value}>
              {window.label}
            </Badge>
          ))}
        </div>
      </div>
    </div>
  )
}
