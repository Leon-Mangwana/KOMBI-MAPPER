/**
 * Client for the Kombi API's network endpoints.
 *
 * The API answers with GeoJSON FeatureCollections, which is also what MapLibre
 * takes as a source body — so nothing here reshapes a response. The types below
 * describe what the server already sends; see api/app/routers/network.py.
 */

const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000').replace(
  /\/+$/,
  '',
)

/** A stop as it appears inside a route's ordered stop list. */
export interface RouteStop {
  seq: number
  id: number
  name: string | null
  lon: number
  lat: number
}

export interface RouteProperties {
  id: number
  short_name: string | null
  long_name: string | null
  length_m: number | null
  stop_count: number
  trace_count: number
  /** Only present on `GET /routes/{id}` — the list endpoint omits it. */
  stops?: RouteStop[]
}

export interface StopProperties {
  id: number
  name: string | null
  route_ids: number[]
}

export type RouteFeature = GeoJSON.Feature<GeoJSON.LineString | null, RouteProperties>
export type StopFeature = GeoJSON.Feature<GeoJSON.Point, StopProperties>

export type RouteCollection = GeoJSON.FeatureCollection<GeoJSON.LineString | null, RouteProperties>
export type StopCollection = GeoJSON.FeatureCollection<GeoJSON.Point, StopProperties>

/** A request that did not produce a usable answer. `status` is null if it never arrived. */
export class ApiError extends Error {
  readonly status: number | null

  constructor(message: string, status: number | null = null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

/**
 * Where the GTFS feed is downloaded from.
 *
 * Defaults to the API, which serves the file `pipeline/gtfs.py` wrote — correct
 * when both run on one machine, as they do in development.
 *
 * A split deployment breaks that: the API on a hosting platform and the
 * pipeline on an operator's laptop share no filesystem, so `GET /gtfs.zip`
 * there is a permanent 404. Set `VITE_GTFS_URL=/gtfs.zip` and have the export
 * write into `web/public/`, and the feed is served as a static file from this
 * app's own origin instead. See ../../README.md.
 */
export const gtfsUrl = import.meta.env.VITE_GTFS_URL ?? `${API_BASE_URL}/gtfs.zip`

export async function fetchRoutes(signal?: AbortSignal): Promise<RouteCollection> {
  return await getJson<RouteCollection>('/routes', signal)
}

export async function fetchStops(signal?: AbortSignal): Promise<StopCollection> {
  return await getJson<StopCollection>('/stops', signal)
}

export async function fetchRoute(routeId: number, signal?: AbortSignal): Promise<RouteFeature> {
  return await getJson<RouteFeature>(`/routes/${routeId}`, signal)
}

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${API_BASE_URL}${path}`, { signal })
  } catch (error) {
    // Unreachable host, DNS failure, CORS rejection, abort. The message the
    // browser gives is not worth showing a visitor, so the caller substitutes
    // its own — but it is kept here for the console.
    throw new ApiError(error instanceof Error ? error.message : 'Network error')
  }

  if (!response.ok) {
    throw new ApiError(
      (await readDetail(response)) ?? `Request failed (HTTP ${response.status})`,
      response.status,
    )
  }

  return (await response.json()) as T
}

async function readDetail(response: Response): Promise<string | null> {
  try {
    const payload = (await response.json()) as { detail?: unknown }
    if (typeof payload.detail === 'string') return payload.detail
  } catch {
    // Body was empty or not JSON — fall back to the status line.
  }
  return null
}
