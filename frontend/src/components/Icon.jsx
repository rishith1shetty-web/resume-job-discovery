/**
 * The application's icon set.
 *
 * Inline SVG rather than emoji or an icon font. Emoji render differently on
 * every platform, cannot inherit colour, and read as an unfinished prototype
 * in a product being handed to a client; an icon font would be a network
 * dependency for something this small. These are drawn on a 24px grid with a
 * consistent 1.75 stroke so they sit together as one family.
 *
 * Every icon is decorative by default (`aria-hidden`) because it always
 * accompanies a visible label. Where an icon is the only content of a control,
 * the control itself carries the accessible name.
 */

const paths = {
  dashboard: (
    <>
      <rect x="3" y="3" width="7" height="9" rx="1.5" />
      <rect x="14" y="3" width="7" height="5" rx="1.5" />
      <rect x="14" y="12" width="7" height="9" rx="1.5" />
      <rect x="3" y="16" width="7" height="5" rx="1.5" />
    </>
  ),
  document: (
    <>
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
      <path d="M14 2v6h6" />
      <path d="M8 13h8M8 17h5" />
    </>
  ),
  insight: (
    <>
      <path d="M12 3a6 6 0 0 0-4 10.47V17a2 2 0 0 0 2 2h4a2 2 0 0 0 2-2v-3.53A6 6 0 0 0 12 3Z" />
      <path d="M9.5 22h5" />
      <path d="M12 9v4" />
    </>
  ),
  search: (
    <>
      <circle cx="11" cy="11" r="7" />
      <path d="m20 20-3.6-3.6" />
    </>
  ),
  history: (
    <>
      <path d="M16 3H8a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V5a2 2 0 0 0-2-2Z" />
      <path d="M9 3h6v3H9z" />
      <path d="M9 11h6M9 15h4" />
    </>
  ),
  upload: (
    <>
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
      <path d="m7 9 5-5 5 5" />
      <path d="M12 4v12" />
    </>
  ),
  check: <path d="m4 12 5.5 5.5L20 7" />,
  checkCircle: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="m8.5 12 2.5 2.5L16 9.5" />
    </>
  ),
  close: <path d="M18 6 6 18M6 6l12 12" />,
  warning: (
    <>
      <path d="M10.3 3.9 1.9 18a2 2 0 0 0 1.7 3h16.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" />
      <path d="M12 9v4M12 17h.01" />
    </>
  ),
  info: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 16v-4M12 8h.01" />
    </>
  ),
  refresh: (
    <>
      <path d="M21 12a9 9 0 1 1-2.6-6.4" />
      <path d="M21 3v6h-6" />
    </>
  ),
  external: (
    <>
      <path d="M15 3h6v6" />
      <path d="M10 14 21 3" />
      <path d="M21 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5" />
    </>
  ),
  pin: (
    <>
      <path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z" />
      <circle cx="12" cy="10" r="3" />
    </>
  ),
  calendar: (
    <>
      <rect x="3" y="5" width="18" height="16" rx="2" />
      <path d="M3 10h18M8 3v4M16 3v4" />
    </>
  ),
  building: (
    <>
      <path d="M4 21V6a2 2 0 0 1 2-2h6a2 2 0 0 1 2 2v15" />
      <path d="M14 10h4a2 2 0 0 1 2 2v9" />
      <path d="M2 21h20" />
      <path d="M7.5 8h3M7.5 12h3M7.5 16h3" />
    </>
  ),
  briefcase: (
    <>
      <rect x="2.5" y="7" width="19" height="14" rx="2" />
      <path d="M8.5 7V5a2 2 0 0 1 2-2h3a2 2 0 0 1 2 2v2" />
      <path d="M2.5 13h19" />
    </>
  ),
  trending: (
    <>
      <path d="m3 16 5.5-5.5 3.5 3.5L21 5" />
      <path d="M15 5h6v6" />
    </>
  ),
  award: (
    <>
      <circle cx="12" cy="9" r="6" />
      <path d="m8.2 14.3-1.5 7 5.3-2.8 5.3 2.8-1.5-7" />
    </>
  ),
  users: (
    <>
      <path d="M16 20v-1.5a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4V20" />
      <circle cx="9" cy="7" r="3.5" />
      <path d="M22 20v-1.5a4 4 0 0 0-3-3.87" />
      <path d="M16.5 3.6a4 4 0 0 1 0 6.8" />
    </>
  ),
  send: (
    <>
      <path d="M21.5 2.5 11 13" />
      <path d="M21.5 2.5 15 21.5l-4-8.5-8.5-4Z" />
    </>
  ),
  menu: <path d="M3 6h18M3 12h18M3 18h18" />,
  chevronRight: <path d="m9 5 7 7-7 7" />,
  bookmark: <path d="M19 21 12 16.5 5 21V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2Z" />,
  shield: (
    <>
      <path d="M12 22s8-4 8-10V5.5L12 2 4 5.5V12c0 6 8 10 8 10Z" />
      <path d="m9 12 2 2 4-4" />
    </>
  ),
  filter: <path d="M3 4h18l-7 8v7l-4 2v-9Z" />,
  inbox: (
    <>
      <path d="M21 12h-6l-2 3h-2l-2-3H3" />
      <path d="M5.5 5h13l2.5 7v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-5Z" />
    </>
  ),
  sparkle: (
    <>
      <path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9Z" />
      <path d="M19 15.5l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8Z" />
    </>
  ),
  clock: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5.2l3.2 1.9" />
    </>
  ),
  target: (
    <>
      <circle cx="12" cy="12" r="9" />
      <circle cx="12" cy="12" r="5" />
      <circle cx="12" cy="12" r="1.3" />
    </>
  ),
  layers: (
    <>
      <path d="m12 2.5 9 5-9 5-9-5Z" />
      <path d="m3 16.5 9 5 9-5" />
      <path d="m3 12 9 5 9-5" />
    </>
  ),
  ban: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="m5.6 5.6 12.8 12.8" />
    </>
  ),
  flag: (
    <>
      <path d="M4 21V4" />
      <path d="M4 4.5h10.5l-1.5 4 1.5 4H4" />
    </>
  ),
  graduation: (
    <>
      <path d="m12 3 10 5-10 5L2 8Z" />
      <path d="M6 10.5V16c0 1.5 2.7 3 6 3s6-1.5 6-3v-5.5" />
    </>
  ),
}

export function Icon({ name, size = 18, className = '', title, ...rest }) {
  const shape = paths[name]
  if (!shape) return null

  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      role={title ? 'img' : undefined}
      aria-hidden={title ? undefined : true}
      focusable="false"
      {...rest}
    >
      {title && <title>{title}</title>}
      {shape}
    </svg>
  )
}

export const ICON_NAMES = Object.keys(paths)
