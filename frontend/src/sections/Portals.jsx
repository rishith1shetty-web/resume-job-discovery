import { listPortals } from '../api/endpoints.js'
import { useAsync } from '../hooks/useAsync.js'
import {
  AsyncBlock,
  Badge,
  Button,
  Card,
  EmptyState,
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

      <SearchPlaceholder portalCount={items.filter((portal) => portal.is_selectable).length} />
    </div>
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
