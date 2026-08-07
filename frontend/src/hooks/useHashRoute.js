import { useCallback, useEffect, useState } from 'react'

/**
 * Hash routing, without a router dependency.
 *
 * The product is five screens with no nested or parameterised routes, so a
 * routing library would be weight without benefit. What it does buy — and what
 * the previous `useState` navigation lacked — is that each screen has its own
 * URL: the browser's back button works, a screen can be bookmarked, and a
 * reload returns to where the user was instead of to the first tab.
 *
 * Hash rather than History API because the built app is served as static files
 * with no server-side rewrite; a deep path would 404 on refresh.
 */
export function useHashRoute(defaultRoute) {
  const read = useCallback(() => {
    const raw = window.location.hash.replace(/^#\/?/, '').split('?')[0]
    return raw || defaultRoute
  }, [defaultRoute])

  const [route, setRoute] = useState(read)

  useEffect(() => {
    const onChange = () => setRoute(read())
    window.addEventListener('hashchange', onChange)
    return () => window.removeEventListener('hashchange', onChange)
  }, [read])

  const navigate = useCallback((next) => {
    if (window.location.hash === `#/${next}`) return
    window.location.hash = `#/${next}`
  }, [])

  return [route, navigate]
}
