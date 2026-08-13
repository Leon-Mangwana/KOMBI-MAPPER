/**
 * The canonical network: every route and stop the pipeline has clustered.
 *
 * Loaded once on mount. It only changes when somebody runs the pipeline again,
 * so there is nothing to poll for and no cache to invalidate — a visitor who
 * wants the newest network reloads the page.
 */
import { defineStore } from 'pinia'
import { computed, ref, shallowRef } from 'vue'

import {
  ApiError,
  fetchRoute,
  fetchRoutes,
  fetchStops,
  type RouteCollection,
  type RouteFeature,
  type RouteProperties,
  type RouteStop,
  type StopCollection,
  type StopFeature,
} from '@/lib/api'
import { isNamed, routeName } from '@/lib/format'

type LoadStatus = 'idle' | 'loading' | 'ready' | 'error'

function collection<T>(features: T[]): { type: 'FeatureCollection'; features: T[] } {
  return { type: 'FeatureCollection', features }
}

export const useNetworkStore = defineStore('network', () => {
  // Route and stop features are large and never mutated in place — only ever
  // swapped wholesale — so they do not need deep reactivity.
  const routes = shallowRef<RouteFeature[]>([])
  const stops = shallowRef<StopFeature[]>([])

  const status = ref<LoadStatus>('idle')
  const error = ref<string | null>(null)

  const selectedId = ref<number | null>(null)
  const detail = shallowRef<RouteFeature | null>(null)
  const detailLoading = ref(false)

  /** Details already fetched, so re-selecting a route costs no request. */
  const detailCache = new Map<number, RouteFeature>()
  let detailRequest: AbortController | null = null

  const routeCollection = computed<RouteCollection>(() => collection(routes.value))
  const stopCollection = computed<StopCollection>(() => collection(stops.value))

  /**
   * Named routes first and alphabetically, then the unnamed ones by id.
   *
   * Naming is manual, so the named routes are the ones a person has already
   * reviewed — the useful half of the list, and the half a visitor can search.
   */
  const orderedRoutes = computed<RouteProperties[]>(() =>
    routes.value
      .map((feature) => feature.properties)
      .sort((a, b) => {
        if (isNamed(a) !== isNamed(b)) return isNamed(a) ? -1 : 1
        if (isNamed(a)) return routeName(a).localeCompare(routeName(b))
        return a.id - b.id
      }),
  )

  const selectedRoute = computed<RouteProperties | null>(
    () => routes.value.find((feature) => feature.properties.id === selectedId.value)?.properties ?? null,
  )

  /** The selected route's stops, in travel order. Empty until the detail lands. */
  const selectedStops = computed<RouteStop[]>(() => detail.value?.properties.stops ?? [])

  const selectedStopIds = computed<number[]>(() => selectedStops.value.map((stop) => stop.id))

  const isEmpty = computed(() => status.value === 'ready' && routes.value.length === 0)

  const totals = computed(() => ({
    routes: routes.value.length,
    stops: stops.value.length,
    traces: routes.value.reduce((sum, feature) => sum + feature.properties.trace_count, 0),
  }))

  async function load(): Promise<void> {
    status.value = 'loading'
    error.value = null

    try {
      // Both in flight at once: neither depends on the other, and the map
      // cannot draw until it has both.
      const [routeData, stopData] = await Promise.all([fetchRoutes(), fetchStops()])
      routes.value = routeData.features
      stops.value = stopData.features
      status.value = 'ready'
    } catch (cause) {
      error.value = describe(cause)
      status.value = 'error'
    }
  }

  async function select(routeId: number | null): Promise<void> {
    // Cancel a detail still in flight for a route the visitor has moved on
    // from, so a slow response cannot overwrite a newer selection.
    detailRequest?.abort()
    detailRequest = null

    selectedId.value = routeId
    if (routeId === null) {
      detail.value = null
      return
    }

    const cached = detailCache.get(routeId)
    if (cached) {
      detail.value = cached
      return
    }

    // The line is already drawn from the list response; only the stops are
    // missing. Keeping the previous detail on screen would show another
    // route's stops, so it goes now and the panel shows its loading state.
    detail.value = null
    detailLoading.value = true
    const request = new AbortController()
    detailRequest = request

    try {
      const feature = await fetchRoute(routeId, request.signal)
      detailCache.set(routeId, feature)
      if (selectedId.value === routeId) detail.value = feature
    } catch (cause) {
      if (request.signal.aborted) return
      error.value = describe(cause)
    } finally {
      if (detailRequest === request) {
        detailRequest = null
        detailLoading.value = false
      }
    }
  }

  function clearSelection(): void {
    void select(null)
  }

  return {
    routes,
    stops,
    status,
    error,
    selectedId,
    detail,
    detailLoading,
    routeCollection,
    stopCollection,
    orderedRoutes,
    selectedRoute,
    selectedStops,
    selectedStopIds,
    isEmpty,
    totals,
    load,
    select,
    clearSelection,
  }
})

/** A message worth showing a visitor, rather than the browser's fetch wording. */
function describe(cause: unknown): string {
  if (cause instanceof ApiError && cause.status !== null) {
    return `The server answered ${cause.status}. ${cause.message}`
  }
  return 'Could not reach the route server. It may be offline — try again shortly.'
}
