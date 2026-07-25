# Kombi Capture

Installable PWA for recording a GPS trace while riding a kombi. One screen: a
MapLibre map showing your position and the trace so far, and one big
**Start/Stop** button.

Stack: Vue 3 (`<script setup>`) · TypeScript · Vite · Pinia · vite-plugin-pwa ·
MapLibre GL JS · idb.

## Setup

```bash
cd capture
npm install
cp .env.example .env    # point VITE_API_BASE_URL at the API
npm run dev
```

`http://localhost:5173` is already in the API's default `CORS_ORIGINS`, so the
dev server can upload with no extra setup. Serving this app from any other
origin (a tunnel, a deployed host) means adding that origin to `CORS_ORIGINS`
in `api/.env`.

### Testing on a real phone

Geolocation and the Wake Lock API need a **secure context** — HTTPS, or
`localhost`. `npm run dev:lan` exposes the dev server on your LAN, but
`http://192.168.x.x:5173` is *not* a secure context, so GPS will not start.
Use a tunnel (`cloudflared tunnel --url http://localhost:5173`, `ngrok http
5173`) or serve the built app over HTTPS.

## Basemap

Protomaps only — this project does not use Mapbox. Set **one** of these in
`.env`:

| Variable                 | Meaning                                                   |
| ------------------------ | --------------------------------------------------------- |
| `VITE_BASEMAP_STYLE_URL` | A self-hosted MapLibre style JSON. Takes priority.         |
| `VITE_PMTILES_URL`       | A `.pmtiles` archive, read via the `pmtiles://` protocol.  |

With `VITE_PMTILES_URL`, the app builds a minimal label-free style (earth,
landuse, water, buildings, roads) so no glyph server is required. For proper
cartography with labels, host a full style JSON and use
`VITE_BASEMAP_STYLE_URL`.

With neither set, the map falls back to a blank background. Your position and
the live trace still render — enough to confirm recording works, just with no
map underneath.

## How recording works

**Points are never buffered in memory.** A phone in a pocket for a 40-minute
ride will suspend or kill the tab, and anything held only in a Pinia ref dies
with it. Every position is written to IndexedDB (via `idb`) as it arrives:

- `navigator.geolocation.watchPosition` with `enableHighAccuracy: true` and
  `maximumAge: 0` — never a cached fix.
- Each point is appended to the `points` store, and the trace's `pointCount` is
  bumped **in the same transaction**, so the counter cannot drift from what is
  on disk.
- A **Screen Wake Lock** is held while recording. The browser drops the lock
  whenever the page is hidden and does not restore it, so it is re-acquired on
  every `visibilitychange` back to visible. If the lock is refused, recording
  continues and the header shows *Screen may sleep*.
- On **Stop**, the trace is marked `pending` and `POST`ed to `/traces`.

Points are stored raw — no smoothing, no accuracy filtering. Cleaning and
outlier rejection are the pipeline's job, and it can only do them properly with
the full observation (`accuracy_m`, `speed_mps`, timestamp).

### Upload queue

A finished trace stays in IndexedDB until the API confirms it.

- Upload failure from network/timeout/5xx → the trace stays `pending`, and the
  queue is retried when `window` fires `online` (or via **Retry now**).
- A 4xx rejection (other than 408/425/429) will never succeed on retry, so the
  trace is marked `failed` and listed with **Retry** / **Discard** buttons.
- On success the points are deleted and a small metadata receipt is kept.
- On launch, any trace still marked `recording` or `uploading` is evidence the
  tab was killed mid-session. Its points are already on disk, so it is
  re-queued rather than thrown away.

### Failure states the UI handles

| Situation                     | What you see                                                  |
| ----------------------------- | ------------------------------------------------------------- |
| Permission denied             | Recording stops, *Location denied* chip, settings hint         |
| No fix yet                    | *Waiting for a GPS fix…*, `0` points, timer still running      |
| `POSITION_UNAVAILABLE`        | *No GPS signal* — keeps watching, does not stop the recording  |
| No Geolocation / insecure origin | *No GPS* chip explaining HTTPS is required                  |
| Offline at Stop               | Trace queues locally; uploads when the connection returns      |

## Data shape

Uploaded to `POST /traces` exactly as the API expects:

```json
{
  "contributor": "leon",
  "points": [
    { "lat": -17.8292, "lon": 31.0522, "recorded_at": "2026-07-24T08:00:00.000Z",
      "accuracy_m": 5, "speed_mps": 8.3 }
  ]
}
```

Coordinates are WGS 84 (EPSG:4326), straight from the Geolocation API. The app
does no metric geometry at all — every distance calculation happens in the
pipeline, reprojected to EPSG:32736. See `../CLAUDE.md`.

## PWA notes

- `registerType: 'autoUpdate'`; the app shell is precached, so it launches
  offline. Map tiles are **not** precached — an offline launch shows the blank
  background style until tiles are reachable.
- `devOptions.enabled` is on, so install and offline behaviour can be exercised
  with `npm run dev`.
- Icons are SVG (`public/icon.svg`). Chrome accepts SVG for installability; add
  192px and 512px PNGs to `public/` and list them in the `manifest.icons` array
  in `vite.config.ts` if you need wider platform coverage.

## Scripts

```bash
npm run dev        # dev server
npm run dev:lan    # dev server on the LAN (see the secure-context caveat above)
npm run build      # typecheck + production build
npm run preview    # serve the production build
npm run typecheck  # vue-tsc --noEmit
```
