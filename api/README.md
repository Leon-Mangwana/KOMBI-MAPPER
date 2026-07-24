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

## Tests

Tests need a disposable PostGIS database. Create one, then run pytest:

```bash
createdb kombi_test   # or: docker compose exec postgis createdb -U postgres kombi_test
export TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/kombi_test
pytest
```

## Notes

- Geometry is stored as EPSG:4326. Reproject to EPSG:32736 (UTM 36S) for any
  metric calculation — never compute distances in degrees. See `../CLAUDE.md`.
