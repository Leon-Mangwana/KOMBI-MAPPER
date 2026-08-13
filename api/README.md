# Kombi API

FastAPI backend for Kombi Route Mapper: trace ingest and route/stop serving.

Stack: FastAPI · SQLAlchemy 2.0 · GeoAlchemy2 · Alembic · PostGIS.

## Setup

```bash
cd api
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env
```

Bring up PostGIS (from the repo root):

```bash
cd ../infra && docker compose up -d
```

## Migrations

```bash
alembic upgrade head                              # apply migrations
alembic revision --autogenerate -m "describe it"  # generate a new migration
```

## Run

```bash
uvicorn app.main:app --reload
```

Interactive docs at http://localhost:8000/docs.

### Endpoints

- `POST /traces` — ingest a trace. Body:
  ```json
  {
    "contributor": "leon",
    "points": [
      {"lat": -17.8292, "lon": 31.0522, "recorded_at": "2026-07-24T08:00:00Z",
       "accuracy_m": 5.0, "speed_mps": 8.3}
    ]
  }
  ```
  All points are bulk-inserted in a single transaction.
- `GET /traces/{id}` — returns the trace as a GeoJSON `Feature` (a `LineString`
  of its ordered points, with per-point attributes in `properties.points`).
- `GET /routes` — every canonical route as a GeoJSON `FeatureCollection`.
  `?include_geometry=false` drops the lines for a light index of the network.
- `GET /routes/{id}` — one route as a `Feature`, with its stops in travel order
  in `properties.stops`.
- `GET /stops` — every stop as a `FeatureCollection`, each carrying the
  `route_ids` that serve it.
- `GET /gtfs.zip` — the exported GTFS feed. See below.

The network endpoints answer with GeoJSON because that is what MapLibre takes as
a source body: [`../web`](../web/README.md) hands the response straight to the
map with no reshaping. They are unpaged — the whole network is a few dozen
routes for one city, and a map draws all of them at once.

`routes`, `stops` and `route_stops` are written by
[`../pipeline/cluster.py`](../pipeline/README.md#clustering) and only read here,
the reverse of `traces`. A response describes the last clustering run and is
stable until the next one. `short_name`, `long_name` and `stops.name` are the
columns clustering cannot fill in — they stay null until somebody names them.

## GTFS

The feed is **built** by [`../pipeline/gtfs.py`](../pipeline/README.md) and only
**served** here. That split is deliberate: building means clustering-scale work
over every route, stop and trace in the database, using the pipeline's GeoPandas
and Shapely stack that this package does not depend on, and the result changes
only when clustering runs — not per request.

So export first, then serve:

```bash
cd ../pipeline && python gtfs.py     # writes pipeline/gtfs_out/gtfs.zip
```

`GTFS_PATH` tells the API where that file is. It defaults to the pipeline's own
default output path resolved relative to this repository, so in a monorepo
checkout the two already agree; set it when they do not share a filesystem in
the obvious way.

Until a feed has been exported the endpoint is a 404, with a message saying so —
from the client's side there is no feed at that URL yet, and the fix is an
export rather than a retry.

Consumers poll a GTFS URL on a timer, so the response carries `ETag` and
`Last-Modified` taken from the file and a matching `If-None-Match` gets a 304
instead of several megabytes. Exports are byte-identical when the network has
not changed, so re-running the pipeline over unchanged data does not invalidate
anybody's cached copy.

## Tests

Tests need a disposable PostGIS database. Create one, then run pytest:

```bash
createdb kombi_test   # or: docker compose exec postgis createdb -U postgres kombi_test
export TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/kombi_test
pytest
```

## CORS

Both front ends call the API with `fetch()` from their own origins, so those
origins must be allowed or the browser blocks the requests. Set `CORS_ORIGINS`
in `.env` (comma-separated); it defaults to the Vite dev server and
`vite preview` for each app — capture on 5173, the web map on 5174:

```
CORS_ORIGINS=http://localhost:5173,http://localhost:4173,http://localhost:5174,http://localhost:4174
```

Testing capture on a phone through a tunnel means adding that URL too, e.g.
`https://something.trycloudflare.com`. Credentials are not allowed — no request
carries cookies or auth headers.

## Notes

- The API never builds the GTFS feed; it serves the file the pipeline wrote.
  Validating that file is documented in
  [`../pipeline/README.md`](../pipeline/README.md#validating-the-feed).
- Geometry is stored as EPSG:4326. Reproject to EPSG:32736 (UTM 36S) for any
  metric calculation — never compute distances in degrees. See `../CLAUDE.md`.
