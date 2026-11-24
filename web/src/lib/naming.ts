/**
 * Which route an operator should name next.
 *
 * Naming is a pass over the unnamed routes, and the panel's "Next unnamed
 * route" button is what moves the pass along. Pure, so it is unit-tested
 * without a store or a map.
 */

import type { RouteProperties } from './api'
import { isNamed } from './format'

/**
 * The id of the next unnamed route after `afterId`, in the order given.
 *
 * `ordered` is the panel's list order (`orderedRoutes`: named first, then
 * unnamed by id), so "next" matches what the operator sees. The search wraps
 * round to the first unnamed route, and starts from it when `afterId` is null,
 * names a route that is not unnamed (just named, say) or is not in the list
 * at all. Null when every route is named.
 */
export function nextUnnamedId(ordered: RouteProperties[], afterId: number | null): number | null {
  const unnamed = ordered.filter((route) => !isNamed(route))
  if (unnamed.length === 0) return null

  const position = afterId === null ? -1 : unnamed.findIndex((route) => route.id === afterId)
  const next = unnamed[(position + 1) % unnamed.length]
  return next?.id ?? null
}
