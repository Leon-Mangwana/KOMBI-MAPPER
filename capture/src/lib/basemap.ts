/**
 * Basemap resolution. Protomaps only — no Mapbox anywhere in this project.
 *
 * Three ways to get a map underneath the trace, in priority order:
 *   1. `VITE_BASEMAP_STYLE_URL` — a full self-hosted MapLibre style JSON.
 *   2. `VITE_PMTILES_URL` — a Protomaps archive, read over `pmtiles://` with
 *      the minimal label-free style below (no glyph server needed).
 *   3. Neither — a blank background. Position and trace still render, which is
 *      all a contributor strictly needs to confirm the app is working.
 */
import maplibregl, { type StyleSpecification } from 'maplibre-gl'
import { Protocol } from 'pmtiles'

const BACKGROUND = '#0f1729'

let protocolRegistered = false

export function registerPmtilesProtocol(): void {
  if (protocolRegistered) return
  const protocol = new Protocol()
  maplibregl.addProtocol('pmtiles', protocol.tile)
  protocolRegistered = true
}

function blankStyle(): StyleSpecification {
  return {
    version: 8,
    sources: {},
    layers: [
      { id: 'background', type: 'background', paint: { 'background-color': BACKGROUND } },
    ],
  }
}

function pmtilesStyle(url: string): StyleSpecification {
  registerPmtilesProtocol()
  return {
    version: 8,
    sources: {
      protomaps: {
        type: 'vector',
        url: `pmtiles://${url}`,
        attribution: '© <a href="https://openstreetmap.org">OpenStreetMap</a>',
      },
    },
    layers: [
      { id: 'background', type: 'background', paint: { 'background-color': BACKGROUND } },
      {
        id: 'earth',
        type: 'fill',
        source: 'protomaps',
        'source-layer': 'earth',
        paint: { 'fill-color': '#141d33' },
      },
      {
        id: 'landuse',
        type: 'fill',
        source: 'protomaps',
        'source-layer': 'landuse',
        paint: { 'fill-color': '#16233d', 'fill-opacity': 0.6 },
      },
      {
        id: 'water',
        type: 'fill',
        source: 'protomaps',
        'source-layer': 'water',
        paint: { 'fill-color': '#0b2f45' },
      },
      {
        id: 'buildings',
        type: 'fill',
        source: 'protomaps',
        'source-layer': 'buildings',
        minzoom: 14,
        paint: { 'fill-color': '#1e2b47' },
      },
      {
        id: 'roads',
        type: 'line',
        source: 'protomaps',
        'source-layer': 'roads',
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: {
          'line-color': '#33456b',
          'line-width': ['interpolate', ['exponential', 1.6], ['zoom'], 8, 0.5, 14, 2, 18, 8],
        },
      },
    ],
  }
}

export function resolveStyle(): string | StyleSpecification {
  const styleUrl = import.meta.env.VITE_BASEMAP_STYLE_URL
  if (styleUrl) {
    // A hand-authored style may itself reference pmtiles:// sources.
    registerPmtilesProtocol()
    return styleUrl
  }

  const pmtilesUrl = import.meta.env.VITE_PMTILES_URL
  if (pmtilesUrl) return pmtilesStyle(pmtilesUrl)

  return blankStyle()
}

export const INITIAL_VIEW = {
  // Harare city centre.
  center: [
    Number(import.meta.env.VITE_MAP_CENTER_LON ?? 31.0522),
    Number(import.meta.env.VITE_MAP_CENTER_LAT ?? -17.8292),
  ] as [number, number],
  zoom: Number(import.meta.env.VITE_MAP_ZOOM ?? 12),
}
