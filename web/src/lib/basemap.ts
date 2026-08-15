/**
 * Basemap resolution. Protomaps only — no Mapbox anywhere in this project.
 *
 * Three ways to get a map underneath the routes, in priority order:
 *   1. `VITE_BASEMAP_STYLE_URL` — a full self-hosted MapLibre style JSON.
 *   2. `VITE_PMTILES_URL` — a Protomaps archive, read over `pmtiles://` with
 *      the minimal label-free style below (no glyph server needed).
 *   3. Neither — a blank background. The routes still draw, which is enough to
 *      check the pipeline's output without hosting tiles first.
 *
 * This is deliberately a near-copy of `capture/src/lib/basemap.ts`. The two
 * apps deploy separately and share no package, and the alternative — a fourth
 * workspace holding one file — would cost more than the duplication does. The
 * colours differ: this map is a reference document people read, the capture one
 * is a backdrop for a live position.
 */
import maplibregl, { type LayerSpecification, type StyleSpecification } from 'maplibre-gl'
import { Protocol } from 'pmtiles'

const BACKGROUND = '#0b1220'

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
    layers: [{ id: 'background', type: 'background', paint: { 'background-color': BACKGROUND } }],
  }
}

/**
 * Place and street labels, drawn on top of everything else.
 *
 * Only included when `VITE_GLYPHS_URL` points at a font source. Text in a
 * MapLibre style needs glyph PBFs, and a style that asks for `text-field`
 * without a `glyphs` URL renders no labels while logging an error per tile —
 * so labels are all-or-nothing and the default stays label-free.
 *
 * Font stack is Noto Sans, which is what Protomaps' own assets bundle publishes.
 * A different glyph source means changing these names to match what it has.
 */
function labelLayers(): LayerSpecification[] {
  return [
    {
      // Street names only close in. At city zooms the route lines are the
      // subject, and road labels under them are noise.
      id: 'road-labels',
      type: 'symbol',
      source: 'protomaps',
      'source-layer': 'roads',
      minzoom: 13,
      filter: ['has', 'name'],
      layout: {
        'symbol-placement': 'line',
        'text-field': ['get', 'name'],
        'text-font': ['Noto Sans Regular'],
        'text-size': 11,
      },
      paint: {
        'text-color': '#93a5c4',
        // A halo in the background colour is what keeps a label readable where
        // it crosses a route line.
        'text-halo-color': BACKGROUND,
        'text-halo-width': 1.4,
      },
    },
    {
      id: 'place-labels',
      type: 'symbol',
      source: 'protomaps',
      'source-layer': 'places',
      // Verified against the Harare extract's own attributes rather than
      // guessed from the schema docs: `pmtiles show --metadata` for the layer
      // names, and a decoded z12 tile for the `kind` vocabulary. Country and
      // region labels are excluded — "Zimbabwe" across a city map is noise.
      filter: [
        'in',
        ['get', 'kind'],
        ['literal', ['locality', 'macrohood', 'neighbourhood', 'suburb', 'quarter']],
      ],
      layout: {
        'text-field': ['get', 'name'],
        'text-font': ['Noto Sans Medium'],
        // Suburb names are what somebody reads a kombi map for — "does this go
        // to Highfield?" — so they grow with zoom rather than staying fine print.
        'text-size': ['interpolate', ['linear'], ['zoom'], 10, 11, 15, 16],
      },
      paint: {
        'text-color': '#dbe4f3',
        'text-halo-color': BACKGROUND,
        'text-halo-width': 1.6,
      },
    },
  ]
}

function pmtilesStyle(url: string): StyleSpecification {
  registerPmtilesProtocol()

  const glyphs = import.meta.env.VITE_GLYPHS_URL

  return {
    version: 8,
    ...(glyphs ? { glyphs } : {}),
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
        paint: { 'fill-color': '#101a2e' },
      },
      {
        id: 'landuse',
        type: 'fill',
        source: 'protomaps',
        'source-layer': 'landuse',
        paint: { 'fill-color': '#132038', 'fill-opacity': 0.55 },
      },
      {
        id: 'water',
        type: 'fill',
        source: 'protomaps',
        'source-layer': 'water',
        paint: { 'fill-color': '#0a2a3d' },
      },
      {
        id: 'buildings',
        type: 'fill',
        source: 'protomaps',
        'source-layer': 'buildings',
        minzoom: 14,
        paint: { 'fill-color': '#1a2740' },
      },
      {
        // Deliberately dim: the roads are context, and the routes drawn on top
        // are the subject. A bright road network competes with them.
        id: 'roads',
        type: 'line',
        source: 'protomaps',
        'source-layer': 'roads',
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: {
          'line-color': '#2a3a5c',
          'line-width': ['interpolate', ['exponential', 1.6], ['zoom'], 8, 0.4, 14, 1.6, 18, 6],
        },
      },
      ...(glyphs ? labelLayers() : []),
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
  zoom: Number(import.meta.env.VITE_MAP_ZOOM ?? 11),
}
