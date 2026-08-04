import { useState } from 'react'

import { listPortals, probePortal } from '../api/endpoints.js'
import { useAsync } from '../hooks/useAsync.js'
import {
  AsyncBlock,
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorState,
  Table,
  orDash,
} from '../components/ui.jsx'

/**
 * Portals are rendered entirely from what the API returns.
 *
 * No portal is named in this file: the list lives in the backend's `portals`
 * table, so adding, enabling or disabling one needs no frontend change.
 */
export function Portals() {
  const portals = useAsync((signal) => listPortals({}, signal), [])
  const items = portals.data?.items ?? []

  return (
    <div className="stack">
      <Card
        title="Portals"
        subtitle="Loaded from GET /portals. Configuration and searching are later phases."
        actions={
          <Button variant="ghost" onClick={portals.run} disabled={portals.loading}>
            Refresh
          </Button>
        }
      >
        <AsyncBlock
          loading={portals.loading}
          error={portals.error}
          data={portals.data}
          onRetry={portals.run}
          isEmpty={(data) => (data.items ?? []).length === 0}
          empty={
            <EmptyState
              title="No portals registered yet."
              hint="The portal list is data, not code. Rows are added to the backend's portals table; this view renders whatever it returns."
            />
          }
        >
          <Table
            columns={[
              'Portal',
              'Slug',
              'Enabled',
              'Implemented',
              'Selectable',
              'Capabilities',
            ]}
            rows={items}
            renderRow={(portal) => (
              <tr key={portal.id}>
                <td>
                  <span className="strong">{portal.display_name}</span>
                  {portal.website_url && (
                    <div>
                      <a
                        className="small"
                        href={portal.website_url}
                        target="_blank"
                        rel="noreferrer noopener"
                      >
                        {portal.website_url}
                      </a>
                    </div>
                  )}
                  {portal.notes && <div className="muted small">{portal.notes}</div>}
                </td>
                <td className="mono small">{portal.slug}</td>
                <td>
                  <Badge tone={portal.is_enabled ? 'good' : 'neutral'}>
                    {portal.is_enabled ? 'Enabled' : 'Disabled'}
                  </Badge>
                </td>
                <td>
                  <Badge tone={portal.is_implemented ? 'good' : 'warn'}>
                    {portal.is_implemented ? 'Implemented' : 'No adapter'}
                  </Badge>
                </td>
                <td>
                  {/* The backend's own rule: enabled AND implemented. */}
                  <Badge tone={portal.is_selectable ? 'good' : 'neutral'}>
                    {portal.is_selectable ? 'Yes' : 'No'}
                  </Badge>
                </td>
                <td>
                  <div className="chip-row">
                    {portal.supports_date_filter && <Badge tone="neutral">Date filter</Badge>}
                    {portal.supports_description_fetch && (
                      <Badge tone="neutral">Descriptions</Badge>
                    )}
                    {portal.requires_auth && <Badge tone="warn">Auth required</Badge>}
                    {!portal.supports_date_filter &&
                      !portal.supports_description_fetch &&
                      !portal.requires_auth &&
                      orDash(null)}
                  </div>
                </td>
              </tr>
            )}
          />
        </AsyncBlock>
      </Card>

      <AdapterProbe portals={items} />

      <SearchPlaceholder portalCount={items.filter((portal) => portal.is_selectable).length} />
    </div>
  )
}

/**
 * Exercise one adapter and show what it collected.
 *
 * The Phase 4 testing affordance: it calls the backend's read-only probe,
 * which caps the result count and stores nothing. It is not the job search —
 * that stays disabled below.
 */
function AdapterProbe({ portals }) {
  const [slug, setSlug] = useState('')
  const [queries, setQueries] = useState('Financial Analyst')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)

  const implemented = portals.filter((portal) => portal.is_implemented)
  const selected = slug || implemented[0]?.slug || ''

  async function run(event) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    setResult(null)
    try {
      setResult(
        await probePortal(selected, {
          queries: queries.split(',').map((q) => q.trim()).filter(Boolean).slice(0, 3),
          limit: 3,
        }),
      )
    } catch (probeError) {
      setError(probeError)
    } finally {
      setBusy(false)
    }
  }

  if (implemented.length === 0) {
    return (
      <Card title="Adapter probe">
        <EmptyState
          title="No adapter is implemented yet."
          hint="Seed the portals and register an adapter first."
        />
      </Card>
    )
  }

  return (
    <Card
      title="Adapter probe"
      subtitle="Runs one adapter against the live portal. Reads only — nothing is stored."
    >
      <form className="form-row" onSubmit={run}>
        <label className="input-group">
          <span>Portal</span>
          <select
            value={selected}
            onChange={(event) => setSlug(event.target.value)}
            className="select"
          >
            {implemented.map((portal) => (
              <option key={portal.id} value={portal.slug}>
                {portal.display_name}
              </option>
            ))}
          </select>
        </label>

        <label className="input-group">
          <span>Search titles (comma separated, max 3)</span>
          <input value={queries} onChange={(event) => setQueries(event.target.value)} />
        </label>

        <Button type="submit" disabled={busy || !queries.trim()}>
          {busy ? 'Probing…' : 'Probe adapter'}
        </Button>
      </form>

      {error && <ErrorState error={error} />}

      {result && !result.ok && (
        <div className="notice notice-warn">
          <strong>{result.portal_slug} did not return listings.</strong>
          <div className="muted small">{result.error_code}</div>
          <p className="small">{result.error_message}</p>
        </div>
      )}

      {result && result.ok && (
        <>
          <p className="muted small">
            Collected {result.collected} in {result.duration_ms} ms.
          </p>
          <Table
            columns={['Title', 'Company', 'Location', 'Posted', 'Description', 'Link']}
            rows={result.jobs}
            renderRow={(job, index) => (
              <tr key={job.external_id ?? index}>
                <td className="strong">{job.title}</td>
                <td>{orDash(job.company)}</td>
                <td>{orDash(job.location)}</td>
                <td>{orDash(job.posted_at ? new Date(job.posted_at).toLocaleString() : null)}</td>
                <td>{job.description_chars ? `${job.description_chars} chars` : orDash(null)}</td>
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
            empty={<EmptyState title="The adapter ran but the portal returned nothing." />}
          />
        </>
      )}
    </Card>
  )
}

/**
 * The search controls, deliberately inert.
 *
 * These do not call POST /search-runs. Nothing here fabricates results or
 * simulates a portal search.
 */
function SearchPlaceholder({ portalCount }) {
  return (
    <Card title="Job search" subtitle="Not yet available">
      <div className="search-controls">
        {[20, 50, 100].map((limit) => (
          <Button key={limit} disabled title="Disabled until the portal adapters exist">
            Find {limit}
          </Button>
        ))}
        <span className="locked-threshold">
          Minimum match: <strong>70%</strong> <Badge tone="neutral">locked</Badge>
        </span>
      </div>

      <p className="notice notice-warn">
        Real job search will be enabled after the Jobright and LinkedIn adapters are
        implemented.
      </p>

      <p className="muted small">
        {portalCount === 0
          ? 'No portal is selectable yet, because no adapter has been implemented.'
          : `${portalCount} portal${portalCount === 1 ? '' : 's'} currently selectable.`}{' '}
        The backend already records search runs and computes per-portal quotas; collection
        is what is missing.
      </p>
    </Card>
  )
}
