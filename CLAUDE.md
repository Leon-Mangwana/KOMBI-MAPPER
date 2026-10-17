# CLAUDE.md

Guidance for Claude Code (and humans) working in this repository.

## Project

**Kombi Route Mapper** — crowdsourced mapping of Harare's informal minibus
("kombi") network. Contributors record GPS traces while riding a kombi. The
backend cleans, map-matches, and clusters those traces into canonical routes
and stops, then exports a valid [GTFS](https://gtfs.org/) feed.

### Pipeline, end to end

1. A contributor rides a kombi and records a GPS trace in the **capture** PWA.
2. The trace is uploaded to the **api**.
3. The **pipeline** cleans the raw points, map-matches them against the road
   network (OSRM), detects stops, and clusters many traces into canonical
   routes and stops.
4. Canonical routes and stops are exported as a valid GTFS feed.
5. The **web** app renders the routes on a public map.

## Monorepo layout

| Path        | Stack                                    | Purpose                                          |
| ----------- | ---------------------------------------- | ------------------------------------------------ |
| `/capture`  | Vue 3 PWA                                | Records and uploads GPS traces                   |
| `/web`      | Vue 3 + MapLibre GL JS                   | Public route map                                 |
| `/api`      | FastAPI + SQLAlchemy 2.0 + GeoAlchemy2   | Ingest traces, serve routes/stops                |
| `/pipeline` | Python                                   | Cleaning, stop detection, clustering, GTFS export |
| `/infra`    | docker-compose                           | Postgres + PostGIS, OSRM                          |

## Conventions

### Geospatial

- Store all geometry as **EPSG:4326** (WGS 84 lon/lat).
- Reproject to **EPSG:32736** (UTM 36S) for **any** metric calculation
  (distances, lengths, areas, buffers, clustering radii).
- **Never compute distances in degrees.** Degrees are not meters.

### Python (`/api`, `/pipeline`)

- Python **3.11**.
- Type hints throughout.
- **ruff** for linting and formatting.
- **pytest** for tests.

### Vue (`/capture`, `/web`)

- Vue 3 **Composition API** with `<script setup>`.
- **Pinia** for state.
- **No Mapbox.** Use MapLibre GL JS with a self-hosted
  [Protomaps](https://protomaps.com/) basemap.

## Getting started

Bring up the infrastructure (Postgres + PostGIS, OSRM):

```bash
cd infra
docker compose up -d postgis   # OSRM needs a prepared dataset first: ./build-osrm.sh
```

Each app directory owns its own tooling and README. See the per-directory
READMEs for run and test instructions.
