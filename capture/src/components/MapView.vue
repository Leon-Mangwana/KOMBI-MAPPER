<script setup lang="ts">
import maplibregl from 'maplibre-gl'
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

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
  const position = store.lastPosition
  if (!map || !position) return

  const lngLat: [number, number] = [position.lon, position.lat]
  if (!marker) {
    marker = new maplibregl.Marker({ element: buildMarkerElement() }).setLngLat(lngLat).addTo(map)
  } else {
    marker.setLngLat(lngLat)
  }

  if (following.value) {
    map.easeTo({ center: lngLat, zoom: Math.max(map.getZoom(), 15), duration: 700 })
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
      id: 'live-trace-line',
      type: 'line',
      source: TRACE_SOURCE,
      layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: { 'line-color': '#38bdf8', 'line-width': 5 },
    })

    syncPath()
    syncPosition()
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
</script>

<template>
  <div class="map">
    <div ref="container" class="map__canvas" aria-label="Map showing your current position"></div>

    <button v-if="!following" type="button" class="map__recenter" @click="recenter">
      Recentre
    </button>

    <div v-if="!store.hasFix" class="map__overlay" role="status">
      <span class="map__overlay-dot" :class="{ 'map__overlay-dot--searching': store.isRecording }" />
      <p v-if="store.gps === 'denied'">Location permission is off — the map cannot follow you.</p>
      <p v-else-if="store.gps === 'unsupported'">This browser cannot provide a GPS position.</p>
      <p v-else-if="store.isRecording">Waiting for a GPS fix…</p>
      <p v-else>No position yet. Press Start to begin recording.</p>
    </div>
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

.map__recenter {
  position: absolute;
  top: 0.75rem;
  right: 0.75rem;
  padding: 0.5rem 0.85rem;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: var(--surface);
  color: var(--text);
  font-size: 0.85rem;
  font-weight: 600;
  box-shadow: 0 2px 10px rgb(0 0 0 / 35%);
}

.map__overlay {
  position: absolute;
  left: 50%;
  bottom: 1rem;
  transform: translateX(-50%);
  display: flex;
  align-items: center;
  gap: 0.5rem;
  max-width: calc(100% - 2rem);
  padding: 0.55rem 0.9rem;
  border-radius: 999px;
  background: rgb(15 23 42 / 92%);
  border: 1px solid var(--line);
  color: var(--text-muted);
  font-size: 0.85rem;
  text-align: center;
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

@media (prefers-reduced-motion: reduce) {
  .position-marker__pulse {
    animation: none;
  }
}
</style>
