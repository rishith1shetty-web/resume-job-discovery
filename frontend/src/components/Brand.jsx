/**
 * The Rising Path Recruiters lockup.
 *
 * `logoUrl` is the official artwork imported as an asset and rendered
 * unmodified — same file, same colours, same proportions, never cropped and
 * never redrawn. The only thing this component decides is how large it is and
 * what sits beside it.
 *
 * One consequence is load-bearing: the supplied artwork's wordmark is cream
 * (#FAF2E8), so it disappears on a light surface. Every placement of this
 * component is therefore on a dark background. That is why the navigation rail
 * and the welcome panel are espresso rather than white.
 */

import logoUrl from '../assets/rising-path-recruiters-logo.png'

export { logoUrl }

export const COMPANY_NAME = 'Rising Path Recruiters'
export const PRODUCT_NAME = 'AI Job Discovery Platform'

/** Full vertical lockup: artwork above the company name. Used in the rail. */
export function BrandLockup({ logoWidth = 132 }) {
  return (
    <div className="sidebar-brand">
      <img
        className="sidebar-logo"
        src={logoUrl}
        alt={`${COMPANY_NAME} logo`}
        style={{ width: logoWidth }}
        width="4167"
        height="4167"
      />
      <div>
        <div className="sidebar-brand-name">{COMPANY_NAME}</div>
        <div className="sidebar-brand-tag">{PRODUCT_NAME}</div>
      </div>
    </div>
  )
}

/** The mark on its own, for the welcome panel. Decorative: the panel's
 *  heading already names the company, so a second announcement would be
 *  noise to a screen reader. */
export function BrandMark({ className = 'hero-art', width = 168 }) {
  return (
    <img
      className={className}
      src={logoUrl}
      alt=""
      aria-hidden="true"
      style={{ width }}
      width="4167"
      height="4167"
    />
  )
}
