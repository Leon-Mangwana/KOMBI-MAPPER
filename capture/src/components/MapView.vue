<script setup lang="ts">
import maplibregl from 'maplibre-gl'
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

import { t } from '@/i18n'
import { INITIAL_VIEW, resolveStyle } from '@/lib/basemap'
import { useRecordingStore } from '@/stores/recording'

import 'maplibre-gl/dist/maplibre-gl.css'

const store = useRecordingStore()

const container = ref<HTMLDivElement | null>(null)
const following = ref(true)

let map: maplibregl.Map | null = null
let marker: maplibregl.Marker | null = null
let styleReady = false

const TRACE_SOURCE = 'live-trace'
const TRACE_LINE_LAYER = 'live-trace-line'
/** The route being recorded, and a finished one left on screen after Stop. */
const TRACE_COLOR_LIVE = '#38bdf8'
const TRACE_COLOR_DONE = '#64748b'

function prefersReducedMotion(): boolean {
  return (
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches
  )
}

function emptyLine(): GeoJSON.Feature<GeoJSON.LineString> {
  return { type: 'Feature', properties: {}, geometry: { type: 'LineString', coordinates: [] } }
}

function buildMarkerElement(): HTMLDivElement {
  const el = document.createElement('div')
  el.className = 'position-marker'
  el.innerHTML = '<span class="position-marker__pulse"></span><span class="position-marker__dot"></span>'
  return el
}

function syncPath(): void {
  if (!map || !styleReady) return
  const source = map.getSource(TRACE_SOURCE) as maplibregl.GeoJSONSource | undefined
  source?.setData({
    type: 'Feature',
    properties: {},
    geometry: { type: 'LineString', coordinates: store.path },
  })
}

function syncPosition(): void {
  if (!map) return
  const position = store.lastPosition
  if (!position) {
    // A new recording has started: the last ride's dot must not look live.
    marker?.remove()
    marker = null
    return
  }

  const lngLat: [number, number] = [position.lon, position.lat]
  if (!marker) {
    marker = new maplibregl.Marker({ element: buildMarkerElement() }).setLngLat(lngLat).addTo(map)
  } else {
    marker.setLngLat(lngLat)
  }
  syncRecordingStyle()

  if (following.value) {
    map.easeTo({
      center: lngLat,
      zoom: Math.max(map.getZoom(), 15),
      duration: prefersReducedMotion() ? 0 : 700,
    })
  }
}

/** After Stop the position is history: grey the dot and mute the line. */
function syncRecordingStyle(): void {
  marker?.getElement().classList.toggle('position-marker--stale', !store.isRecording)
  if (map && styleReady) {
    map.setPaintProperty(
      TRACE_LINE_LAYER,
      'line-color',
      store.isRecording ? TRACE_COLOR_LIVE : TRACE_COLOR_DONE,
    )
  }
}

function recenter(): void {
  following.value = true
  syncPosition()
}

onMounted(() => {
  if (!container.value) return

  map = new maplibregl.Map({
    container: container.value,
    style: resolveStyle(),
    center: INITIAL_VIEW.center,
    zoom: INITIAL_VIEW.zoom,
    attributionControl: { compact: true },
    // Pitch/rotate are noise on a phone held in one hand on a moving kombi.
    pitchWithRotate: false,
    dragRotate: false,
  })

  map.touchZoomRotate.disableRotation()
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-left')

  // Any manual pan means the contributor wants to look around; stop chasing.
  map.on('dragstart', () => {
    following.value = false
  })

  map.on('load', () => {
    if (!map) return
    styleReady = true

    map.addSource(TRACE_SOURCE, { type: 'geojson', data: emptyLine() })
    map.addLayer({
      id: 'live-trace-casing',
      type: 'line',
      source: TRACE_SOURCE,
      layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: { 'line-color': '#0b1220', 'line-width': 9, 'line-opacity': 0.6 },
    })
    map.addLayer({
      id: TRACE_LINE_LAYER,
      type: 'line',
      source: TRACE_SOURCE,
      layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: { 'line-color': TRACE_COLOR_LIVE, 'line-width': 5 },
    })

    syncPath()
    syncPosition()
    syncRecordingStyle()
  })
})

onBeforeUnmount(() => {
  marker?.remove()
  marker = null
  map?.remove()
  map = null
  styleReady = false
})

watch(() => store.path, syncPath, { deep: false })
watch(() => store.lastPosition, syncPosition)
watch(() => store.isRecording, syncRecordingStyle)
</script>

<template>
  <div class="map">
    <div ref="container" class="map__canvas" :aria-label="t.map.label"></div>

    <!-- Only with a fix: then the overlay below is gone and cannot collide. -->
    <button
      v-if="!following && store.hasFix"
      type="button"
      class="map__recenter"
      @click="recenter"
    >
      {{ t.map.recentre }}
    </button>

    <div v-if="!store.hasFix" class="map__overlay" role="status">
      <span class="map__overlay-dot" :class="{ 'map__overlay-dot--searching': store.isRecording }" />
      <p v-if="store.gps === 'denied'">{{ t.map.denied }}</p>
      <p v-else-if="store.gps === 'unsupported'">{{ t.map.unsupported }}</p>
      <p v-else-if="store.isRecording">{{ t.map.finding }}</p>
      <p v-else>{{ t.map.idle }}</p>
    </div>
  </div>
</template>

<style scoped>
.map {
  position: relative;
  /* Grows to fill, never squeezed below a usable size by the panel. */
  flex: 1 1 12rem;
  min-height: 12rem;
}

.map__canvas {
  position: absolute;
  inset: 0;
}

.map__recenter {
  position: absolute;
  top: 0.75rem;
  right: 0.75rem;
  min-height: 2.75rem;
  padding: 0.5rem 0.95rem;
  border: 1px solid var(--line-strong);
  border-radius: 999px;
  background: var(--surface);
  color: var(--text);
  font-size: 0.9rem;
  font-weight: 600;
  box-shadow: 0 2px 10px rgb(0 0 0 / 35%);
}

/* Top centre, between the zoom buttons (top left) and clear of MapLibre's
   attribution (bottom right), which can open wide on a narrow phone. */
.map__overlay {
  position: absolute;
  top: 0.75rem;
  left: 3.25rem;
  right: 3.25rem;
  display: flex;
  align-items: center;
  gap: 0.5rem;
  width: fit-content;
  max-width: calc(100% - 6.5rem);
  margin-inline: auto;
  padding: 0.55rem 0.85rem;
  border-radius: 0.75rem;
  background: rgb(15 23 42 / 92%);
  border: 1px solid var(--line);
  color: var(--text-muted);
  font-size: 0.9rem;
  line-height: 1.35;
}

.map__overlay p {
  margin: 0;
}

.map__overlay-dot {
  flex: none;
  width: 0.5rem;
  height: 0.5rem;
  border-radius: 50%;
  background: var(--text-muted);
}

.map__overlay-dot--searching {
  background: var(--warn);
  animation: blink 1.2s ease-in-out infinite;
}

@keyframes blink {
  50% {
    opacity: 0.25;
  }
}

@media (prefers-reduced-motion: reduce) {
  .map__overlay-dot--searching {
    animation: none;
  }
}
</style>

<style>
/* Marker element lives outside the component root, so it cannot be scoped. */
.position-marker {
  position: relative;
  width: 1.25rem;
  height: 1.25rem;
}

.position-marker__dot,
.position-marker__pulse {
  position: absolute;
  inset: 0;
  border-radius: 50%;
}

.position-marker__dot {
  background: #38bdf8;
  border: 2px solid #f8fafc;
  box-shadow: 0 0 0 1px rgb(0 0 0 / 40%);
}

.position-marker__pulse {
  background: #38bdf8;
  opacity: 0.35;
  animation: position-pulse 2s ease-out infinite;
}

@keyframes position-pulse {
  from {
    transform: scale(1);
    opacity: 0.35;
  }
  to {
    transform: scale(3);
    opacity: 0;
  }
}

/* The ride has stopped: the dot is where you were, not where you are. */
.position-marker--stale .position-marker__dot {
  background: #64748b;
}

.position-marker--stale .position-marker__pulse {
  display: none;
}

@media (prefers-reduced-motion: reduce) {
  .position-marker__pulse {
    animation: none;
  }
}
</style>
