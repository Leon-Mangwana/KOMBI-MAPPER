# Kombi Web

The public map. Renders the canonical routes and stops the pipeline clustered,
and links to the exported GTFS feed.

Stack: Vue 3 (`<script setup>`) · TypeScript · Vite · Pinia · MapLibre GL JS.

## Setup

```bash
cd web
npm install
cp .env.example .env    # point VITE_API_BASE_URL at the API
npm run dev
```

The dev server is pinned to **port 5174** so it can run alongside the capture
PWA on 5173, and `http://localhost:5174` is in the API's default
`CORS_ORIGINS`. Serving this app from any other origin means adding that origin
to `CORS_ORIGINS` in `api/.env`, or the browser will block every request.

There is nothing to see until the pipeline has clustered something — the app
says so rather than showing an empty map with no explanation. To get data in
front of it, upload a few traces and run `python process.py --full` in
`../pipeline`.

## Basemap

Protomaps only — this project does not use Mapbox. Set **one** of these in
`.env`:

| Variable                  | What it is                                            |
| ------------------------- | ----------------------------------------------------- |
| `VITE_BASEMAP_STYLE_URL`  | A full self-hosted MapLibre style JSON                 |
| `VITE_PMTILES_URL`        | A `.pmtiles` archive, read over the `pmtiles://` protocol |

With neither set the app falls back to a blank dark background. The routes and
stops still draw, which is enough to check the pipeline's output before you host
tiles.

The built-in pmtiles style deliberately has no labels, so it needs no glyph
server. Point `VITE_BASEMAP_STYLE_URL` at a style with a `glyphs` URL if you
want place names.

## The GTFS feed in a split deployment

The header's **GTFS feed** link points at the API's `/gtfs.zip` by default,
which is right when the API and the pipeline run on one machine.

They usually don't in production: the API is on a hosting platform and the
pipeline runs on an operator's laptop, so the API has no copy of the file and
that URL is a permanent 404. Export the feed into this app's `public/` folder
instead and serve it from the same origin as the map:

```bash
cd pipeline
python gtfs.py -o ../web/public/gtfs.zip
```

Then set `VITE_GTFS_URL=/gtfs.zip` and redeploy. The feed is committed and ships
with the site, so publishing a new one is a commit and a deploy — which also
gives you a history of what was published when.

## What it draws

| Layer              | What                                                     |
| ------------------ | -------------------------------------------------------- |
| `routes-line`      | Every canonical route, dimmed                             |
| `routes-selected`  | The selected route, in the accent colour                  |
| `stops-all`        | Every stop, small — shown only when nothing is selected   |
| `stops-selected`   | The selected route's stops, ringed and clickable          |

Selecting a route — from the map or the list — fetches `GET /routes/{id}` for
its ordered stops and frames it. Clicking the background clears the selection.

Route and stop names come from `routes.short_name`, `routes.long_name` and
`stops.name`, which **clustering cannot fill in**: a cluster of GPS traces has
no idea it is the Mbare route. Until somebody names them by hand the app falls
back to `Route 12` / `Stop 3`, and shows unnamed routes in a lighter weight so
the named ones read as the reviewed half of the list.

## Operator mode

Route and stop names cannot be clustered — somebody who knows Harare has to
supply them. Doing that for thirty routes through `curl` is how it does not get
done, so the app has a naming UI:

1. Open the site with `#admin` on the end of the URL.
2. Paste the API's `ADMIN_TOKEN` and unlock.
3. Select a route: its short and long name become editable, and each stop in the
   list becomes an inline text box. Changes save on submit and on blur.

Hiding the controls behind `#admin` is **not** the security boundary — the API
checks the token on every write, and refuses without it. It keeps the public map
free of affordances that would only ever reject a visitor.

The token is kept in `sessionStorage`, so it is scoped to the tab and gone when
it closes. That is a deliberate trade against `localStorage`: an operator retypes
it once per session, and a borrowed machine does not keep write access alive.

Names set here survive re-clustering — see
[`../pipeline/README.md`](../pipeline/README.md#naming).

## Data shape

Everything comes from three endpoints, all GeoJSON — the same shape MapLibre
takes as a source body, so nothing in `lib/api.ts` reshapes a response:

- `GET /routes` — FeatureCollection of LineStrings. `?include_geometry=false`
  gives a light index.
- `GET /routes/{id}` — one Feature, with `properties.stops` in travel order.
- `GET /stops` — FeatureCollection of Points, each with the `route_ids` serving it.

Lengths arrive in **metres**, computed server-side in EPSG:32736. Nothing here
computes a distance — see [../CLAUDE.md](../CLAUDE.md).

## Scripts

| Command             | What it does                          |
| ------------------- | ------------------------------------- |
| `npm run dev`       | Dev server on port 5174               |
| `npm run build`     | Type-check, then build to `dist/`     |
| `npm run preview`   | Serve the build on port 4174          |
| `npm run typecheck` | `vue-tsc --noEmit`                    |

## Notes

- `src/lib/basemap.ts` is a near-copy of the capture app's. The two deploy
  separately and share no package; a fourth workspace holding one file would
  cost more than the duplication does.
- The network is loaded once on mount. It only changes when somebody runs the
  pipeline, so there is nothing to poll for — a visitor wanting the newest
  network reloads the page.
