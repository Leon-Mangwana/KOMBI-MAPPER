# Kombi Route Mapper

Crowdsourced mapping of Harare's informal minibus ("kombi") network.

Contributors record GPS traces while riding a kombi. The backend cleans,
map-matches, and clusters those traces into canonical routes and stops, then
exports a valid [GTFS](https://gtfs.org/) feed for use in trip planners and
transit apps.

## How it works

1. **Capture** — ride a kombi, record a GPS trace in the PWA, upload it.
2. **Ingest** — the API stores raw traces.
3. **Process** — `pipeline/process.py` cleans and map-matches (OSRM) every
   uploaded trace, then clusters the matched ones into canonical routes and
   stops, detecting stops from where kombis stood still.
4. **Export** — canonical routes and stops become a GTFS feed, served at
   `GET /gtfs.zip`. Kombis have no timetable, so the feed uses `frequencies.txt`
   with headways measured from observed departures rather than an invented
   schedule — see [pipeline/README.md](pipeline/README.md#gtfs-export).
5. **Explore** — the web app renders the routes on a public map, reading
   `GET /routes` and `GET /stops`.

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

Each directory owns its own tooling and has a README with the detail; this is
the shortest path from a clean checkout to a running system.

**1. Infrastructure.**

```bash
cd infra
docker compose up -d postgis
```

The OSRM service used for map matching needs a road network prepared from an OSM
extract before it will start. `./build-osrm.sh --up` downloads the Zimbabwe
extract and prepares it (several minutes, a few GB) — see
[infra/README.md](infra/README.md). Nothing but map matching needs it, so the
rest can come up first.

**2. API** — [api/README.md](api/README.md)

```bash
cd api
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env
alembic upgrade head
uvicorn app.main:app --reload                       # http://localhost:8000/docs
```

**3. Capture PWA** — [capture/README.md](capture/README.md)

```bash
cd capture
npm install
cp .env.example .env
npm run dev                                         # http://localhost:5173
```

GPS needs a secure context, so testing on a phone means a tunnel or HTTPS — see
the capture README.

**4. Pipeline** — [pipeline/README.md](pipeline/README.md)

Once some traces have been uploaded, and with OSRM running:

```bash
cd pipeline
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
python process.py --full          # match new traces, rebuild the network, re-export GTFS
```

`process.py` is the whole backend in one command. Run the stages separately
(`cluster.py`, `gtfs.py`) when tuning them.

**5. Web map** — [web/README.md](web/README.md)

```bash
cd web
npm install
cp .env.example .env
npm run dev                                         # http://localhost:5174
```

The two front ends are pinned to different ports so they can run at once, and
both are in the API's default `CORS_ORIGINS`. Neither has a basemap until you
point it at a self-hosted Protomaps style or `.pmtiles` archive; without one
they render on a blank background, which is enough to see the data.

## Development

- **Python** (`api`, `pipeline`): Python 3.11, type hints, `ruff`, `pytest`.
- **Vue** (`capture`, `web`): Vue 3 Composition API (`<script setup>`), Pinia,
  MapLibre GL JS (self-hosted Protomaps basemap).

See [CLAUDE.md](CLAUDE.md) for full conventions, including the geospatial
CRS rules (store EPSG:4326, compute in EPSG:32736).

Tests and checks, per directory:

```bash
cd api      && ruff check . && ruff format --check . && pytest   # needs PostGIS
cd pipeline && ruff check . && ruff format --check . && pytest   # no services needed
cd capture  && npm run typecheck && npm run build
cd web      && npm run typecheck && npm run build
```

CI in [.github/workflows](.github/workflows) runs exactly these, one workflow
per directory, triggered only by changes under it. The API workflow brings up a
PostGIS service; the pipeline suite deliberately needs no database, and mocks
OSRM.

## License

<!-- TODO: choose a license -->
