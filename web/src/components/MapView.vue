<script setup lang="ts">
import maplibregl from 'maplibre-gl'
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

import { INITIAL_VIEW, resolveStyle } from '@/lib/basemap'
import { stopName } from '@/lib/format'
import { useNetworkStore } from '@/stores/network'

import 'maplibre-gl/dist/maplibre-gl.css'

const store = useNetworkStore()

const container = ref<HTMLDivElement | null>(null)

let map: maplibregl.Map | null = null
let popup: maplibregl.Popup | null = null
let styleReady = false

const ROUTES = 'routes'
const STOPS = 'stops'

const ROUTE_LAYERS = ['routes-line', 'routes-selected'] as const

/** Padding around a fitted route, in pixels, leaving room for the panel. */
const FIT_PADDING = { top: 60, bottom: 60, left: 60, right: 60 }

function empty<T extends GeoJSON.Geometry>(): GeoJSON.FeatureCollection<T> {
  return { type: 'FeatureCollection', features: [] }
}

function source(id: string): maplibregl.GeoJSONSource | undefined {
  return map?.getSource(id) as maplibregl.GeoJSONSource | undefined
}

function syncData(): void {
  if (!map || !styleReady) return
  source(ROUTES)?.setData(store.routeCollection as GeoJSON.FeatureCollection)
  source(STOPS)?.setData(store.stopCollection as GeoJSON.FeatureCollection)
}

function syncSelection(): void {
  if (!map || !styleReady) return

  const selected = store.selectedId
  // `-1` matches nothing: with no selection the highlight layer draws none of
  // the routes rather than being toggled off, so the layer order never changes.
  map.setFilter('routes-selected', ['==', ['get', 'id'], selected ?? -1])
  map.setFilter('routes-line', ['!=', ['get', 'id'], selected ?? -1])

  const hasSelection = selected !== null
  map.setLayoutProperty('stops-all', 'visibility', hasSelection ? 'none' : 'visible')
  map.setFilter('stops-selected', [
    'in',
    ['get', 'id'],
    ['literal', hasSelection ? store.selectedStopIds : []],
  ])

  popup?.remove()
}

/** Bounds of one route's line, or null if it has no geometry. */
function routeBounds(routeId: number): maplibregl.LngLatBounds | null {
  const feature = store.routes.find((candidate) => candidate.properties.id === routeId)
  const coordinates = feature?.geometry?.coordinates
  if (!coordinates?.length) return null

  return coordinates.reduce(
    (bounds, position) => bounds.extend(position as [number, number]),
    new maplibregl.LngLatBounds(
      coordinates[0] as [number, number],
      coordinates[0] as [number, number],
    ),
  )
}

function frameSelection(): void {
  if (!map || store.selectedId === null) return
  const bounds = routeBounds(store.selectedId)
  if (bounds) map.fitBounds(bounds, { padding: FIT_PADDING, duration: 700, maxZoom: 15 })
}

/** Frame the whole network, so the first view is not an arbitrary city block. */
function frameNetwork(): void {
  if (!map) return

  const bounds = new maplibregl.LngLatBounds()
  let extended = false
  for (const feature of store.routes) {
    for (const position of feature.geometry?.coordinates ?? []) {
      bounds.extend(position as [number, number])
      extended = true
    }
  }

  if (extended) map.fitBounds(bounds, { padding: FIT_PADDING, duration: 0, maxZoom: 14 })
}

defineExpose({ frameNetwork })

function showStopPopup(event: maplibregl.MapMouseEvent & { features?: maplibregl.MapGeoJSONFeature[] }): void {
  if (!map) return
  const feature = event.features?.[0]
  if (!feature || feature.geometry.type !== 'Point') return

  const properties = feature.properties as { id: number; name: string | null }
  const seq = store.selectedStops.findIndex((stop) => stop.id === properties.id)
  const label = stopName(properties.name ?? null, seq === -1 ? 0 : seq)

  popup?.remove()
  popup = new maplibregl.Popup({ closeButton: false, offset: 12 })
    .setLngLat(feature.geometry.coordinates as [number, number])
    .setText(label)
    .addTo(map)
}

function addLayers(): void {
  if (!map) return

  map.addSource(ROUTES, { type: 'geojson', data: empty<GeoJSON.LineString>() })
  map.addSource(STOPS, { type: 'geojson', data: empty<GeoJSON.Point>() })

  // A casing under every line keeps routes legible where they overlap, which
  // in a network converging on one CBD is most of the interesting area.
  map.addLayer({
    id: 'routes-casing',
    type: 'line',
    source: ROUTES,
    layout: { 'line-cap': 'round', 'line-join': 'round' },
    paint: {
      'line-color': '#020617',
      'line-width': ['interpolate', ['linear'], ['zoom'], 9, 3, 14, 8],
      'line-opacity': 0.7,
    },
  })

  map.addLayer({
    id: 'routes-line',
    type: 'line',
    source: ROUTES,
    layout: { 'line-cap': 'round', 'line-join': 'round' },
    paint: {
      'line-color': '#38bdf8',
      'line-width': ['interpolate', ['linear'], ['zoom'], 9, 1.5, 14, 4],
      'line-opacity': 0.55,
    },
  })

  map.addLayer({
    id: 'routes-selected',
    type: 'line',
    source: ROUTES,
    filter: ['==', ['get', 'id'], -1],
    layout: { 'line-cap': 'round', 'line-join': 'round' },
    paint: {
      'line-color': '#fbbf24',
      'line-width': ['interpolate', ['linear'], ['zoom'], 9, 3, 14, 7],
    },
  })

  map.addLayer({
    id: 'stops-all',
    type: 'circle',
    source: STOPS,
    paint: {
      'circle-radius': ['interpolate', ['linear'], ['zoom'], 11, 2, 15, 4],
      'circle-color': '#e2e8f0',
      'circle-opacity': 0.5,
    },
  })

  map.addLayer({
    id: 'stops-selected',
    type: 'circle',
    source: STOPS,
    filter: ['in', ['get', 'id'], ['literal', []]],
    paint: {
      'circle-radius': ['interpolate', ['linear'], ['zoom'], 11, 4, 15, 7],
      'circle-color': '#0b1220',
      'circle-stroke-color': '#fbbf24',
      'circle-stroke-width': 2,
    },
  })
}

function bindInteractions(): void {
  if (!map) return

  for (const layer of ROUTE_LAYERS) {
    map.on('click', layer, (event) => {
      const properties = event.features?.[0]?.properties as { id: number } | undefined
      if (properties) void store.select(properties.id)
    })
    map.on('mouseenter', layer, () => {
      if (map) map.getCanvas().style.cursor = 'pointer'
    })
    map.on('mouseleave', layer, () => {
      if (map) map.getCanvas().style.cursor = ''
    })
  }

  map.on('click', 'stops-selected', showStopPopup)

  // A click on the background clears the selection. Checked by querying the
  // route layers rather than by ordering handlers, because MapLibre fires the
  // layer handler and this one for the same click.
  map.on('click', (event) => {
    if (!map) return
    const hits = map.queryRenderedFeatures(event.point, { layers: [...ROUTE_LAYERS] })
    if (hits.length === 0) store.clearSelection()
  })
}

onMounted(() => {
  if (!container.value) return

  map = new maplibregl.Map({
    container: container.value,
    style: resolveStyle(),
    center: INITIAL_VIEW.center,
    zoom: INITIAL_VIEW.zoom,
    attributionControl: { compact: true },
  })

  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right')
  map.addControl(new maplibregl.ScaleControl({ unit: 'metric' }), 'bottom-left')

  map.on('load', () => {
    if (!map) return
    styleReady = true

    addLayers()
    bindInteractions()
    syncData()
    syncSelection()
    frameNetwork()
  })
})

onBeforeUnmount(() => {
  popup?.remove()
  popup = null
  map?.remove()
  map = null
  styleReady = false
})

watch(
  () => store.routeCollection,
  () => {
    syncData()
    // The first payload decides where the map should be looking; a later one
    // only arrives on a reload, when framing again is still the right answer.
    if (store.selectedId === null) frameNetwork()
  },
)
watch(() => store.stopCollection, syncData)
watch([() => store.selectedId, () => store.selectedStopIds], () => {
  syncSelection()
  frameSelection()
})
</script>

<template>
  <div class="map">
    <div ref="container" class="map__canvas" aria-label="Map of Harare's kombi routes"></div>

    <p v-if="store.status === 'loading'" class="map__status" role="status">Loading routes…</p>
  </div>
</template>

<style scoped>
.map {
  position: relative;
  flex: 1;
  min-height: 0;
}

.map__canvas {
  position: absolute;
  inset: 0;
}

.map__status {
  position: absolute;
  left: 50%;
  top: 1rem;
  transform: translateX(-50%);
  margin: 0;
  padding: 0.45rem 0.9rem;
  border-radius: 999px;
  border: 1px solid var(--line);
  background: rgb(15 23 42 / 92%);
  color: var(--text-muted);
  font-size: 0.85rem;
}
</style>

<style>
/* Popups are appended outside the component root, so they cannot be scoped. */
.maplibregl-popup-content {
  padding: 0.4rem 0.7rem;
  border-radius: 0.5rem;
  background: var(--surface);
  color: var(--text);
  border: 1px solid var(--line);
  font-size: 0.85rem;
}

.maplibregl-popup-tip {
  border-top-color: var(--surface) !important;
  border-bottom-color: var(--surface) !important;
}
</style>
