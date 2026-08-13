/** Presentation helpers. Nothing here computes geography — see CLAUDE.md. */

import type { RouteProperties } from './api'

/**
 * What to call a route.
 *
 * Clustering cannot name anything — a cluster of GPS traces has no idea it is
 * "the Mbare route" — so `short_name` and `long_name` are null until somebody
 * fills them in by hand. Unnamed is the normal state early on, and the fallback
 * has to be something a visitor can still refer to.
 */
export function routeName(route: RouteProperties): string {
  if (route.short_name && route.long_name) return `${route.short_name} — ${route.long_name}`
  return route.short_name ?? route.long_name ?? `Route ${route.id}`
}

/** Whether a route has a human-given name, as opposed to the id fallback. */
export function isNamed(route: RouteProperties): boolean {
  return Boolean(route.short_name || route.long_name)
}

export function formatLength(metres: number | null): string {
  if (metres === null) return '—'
  if (metres < 1000) return `${Math.round(metres)} m`
  return `${(metres / 1000).toFixed(1)} km`
}

export function plural(count: number, singular: string, pluralForm = `${singular}s`): string {
  return `${count} ${count === 1 ? singular : pluralForm}`
}

export function stopName(name: string | null, seq: number): string {
  return name ?? `Stop ${seq + 1}`
}
