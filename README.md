# Kombi Route Mapper

Crowdsourced mapping of Harare's informal minibus ("kombi") network.

Contributors record GPS traces while riding a kombi. The backend cleans,
map-matches, and clusters those traces into canonical routes and stops, then
exports a valid [GTFS](https://gtfs.org/) feed for use in trip planners and
transit apps.

## How it works

1. **Capture** — ride a kombi, record a GPS trace in the PWA, upload it.
2. **Ingest** — the API stores raw traces.
3. **Process** — the pipeline cleans, map-matches (OSRM), detects stops, and
   clusters traces into canonical routes and stops.
4. **Export** — canonical routes and stops become a GTFS feed.
5. **Explore** — the web app renders routes on a public map.

## Repository layout

| Path        | Description                                          |
| ----------- | ---------------------------------------------------- |
| `capture/`  | Vue 3 PWA — records and uploads GPS traces           |
| `web/`      | Vue 3 + MapLibre GL JS — public route map            |
| `api/`      | FastAPI + SQLAlchemy 2.0 + GeoAlchemy2 — backend     |
| `pipeline/` | Python — cleaning, stop detection, clustering, GTFS  |
| `infra/`    | docker-compose — Postgres + PostGIS, OSRM            |

## Quick start

Prerequisites: Docker, Python 3.11, Node.js.

```bash
# 1. Bring up Postgres + PostGIS
cd infra
docker compose up -d postgis
```

The OSRM service used for map matching needs a road network prepared from an OSM
extract before it will start — see [infra/README.md](infra/README.md).

<!-- TODO: document api, pipeline, capture, and web setup once scaffolded -->

## Development

- **Python** (`api`, `pipeline`): Python 3.11, type hints, `ruff`, `pytest`.
- **Vue** (`capture`, `web`): Vue 3 Composition API (`<script setup>`), Pinia,
  MapLibre GL JS (self-hosted Protomaps basemap).

See [CLAUDE.md](CLAUDE.md) for full conventions, including the geospatial
CRS rules (store EPSG:4326, compute in EPSG:32736).

## License

<!-- TODO: choose a license -->
