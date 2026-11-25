/**
 * The URL of a route.
 *
 * A selected route is reflected in the address bar as `?route=12`, so the
 * address can be copied and sent, and opens the map with that route selected.
 * A query parameter rather than the hash: the hash is already the operator
 * switch (`#admin`, see stores/admin.ts), and Netlify's catch-all serves
 * `index.html` for any path or query. Pure functions; the DOM is wired up in
 * App.vue.
 */

const PARAM = 'route'

/** The route id a URL's query asks for, or null if none or not a positive integer. */
export function routeIdFromSearch(search: string): number | null {
  const raw = new URLSearchParams(search).get(PARAM)
  if (raw === null || !/^\d+$/.test(raw)) return null
  const id = Number(raw)
  return id > 0 ? id : null
}

/**
 * The query string with the route set, or removed when `routeId` is null.
 * Other parameters are kept. Empty string when nothing is left, so the address
 * loses its trailing `?`.
 */
export function searchWithRoute(search: string, routeId: number | null): string {
  const params = new URLSearchParams(search)
  if (routeId === null) params.delete(PARAM)
  else params.set(PARAM, String(routeId))
  const encoded = params.toString()
  return encoded ? `?${encoded}` : ''
}

/**
 * The address to hand to somebody for one route: origin and path, the route,
 * and nothing else. Any other query and the hash are dropped on purpose, so a
 * link copied in operator mode does not carry `#admin` with it.
 */
export function shareUrl(origin: string, pathname: string, routeId: number): string {
  return `${origin}${pathname}${searchWithRoute('', routeId)}`
}
