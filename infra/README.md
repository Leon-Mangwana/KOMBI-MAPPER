# Infrastructure

Local backing services for Kombi Route Mapper, via docker-compose.

| Service   | Image                       | Port | Purpose                                     |
| --------- | --------------------------- | ---- | ------------------------------------------- |
| `postgis` | `postgis/postgis:15-3.4`    | 5432 | Traces, routes and stops (the API's database) |
| `osrm`    | `osrm/osrm-backend:v5.27.1` | 5000 | Map matching for [`../pipeline/match.py`](../pipeline/match.py) |

```bash
cd infra
docker compose up -d postgis     # works on a clean checkout
```

`osrm` will **not** start on a clean checkout — `osrm-routed` needs a prepared
road network, and preparing one is a one-off manual step. Do that next.

## Preparing the OSRM dataset

Three commands turn an OSM extract into something `osrm-routed` can serve. They
are run once (and again whenever you refresh the extract), not on every boot.

Everything below runs through `docker compose run`, so the preparation uses the
exact image pinned in [`docker-compose.yml`](docker-compose.yml) and writes into
the same `./data` volume the server reads. Preparing with one OSRM version and
serving with another does not work — `osrm-routed` refuses a dataset whose file
format does not match its own.

### 1. Download the Zimbabwe extract

Geofabrik publishes a daily country extract:

```bash
mkdir -p data
curl -L -o data/zimbabwe-latest.osm.pbf \
  https://download.geofabrik.de/africa/zimbabwe-latest.osm.pbf
```

About 40 MB. `data/` is gitignored — extracts and the `.osrm.*` files built from
them are large and reproducible, so they never get committed.

Optionally verify the download against Geofabrik's published checksum:

```bash
curl -L -o data/zimbabwe-latest.osm.pbf.md5 \
  https://download.geofabrik.de/africa/zimbabwe-latest.osm.pbf.md5
cd data && md5sum -c zimbabwe-latest.osm.pbf.md5 && cd ..
```

The whole-country extract is used rather than a Harare bounding box on purpose:
kombi routes run out to Chitungwiza, Norton and Ruwa, and a trace that leaves
the box would map-match against a road network with a hole in it.

### 2. Extract, partition, customize

```bash
# Build the routing graph with the car profile (~2-4 min, needs ~2 GB RAM).
docker compose run --rm osrm \
  osrm-extract -p /opt/car.lua /data/zimbabwe-latest.osm.pbf

# Multi-level Dijkstra preprocessing.
docker compose run --rm osrm osrm-partition /data/zimbabwe-latest.osrm
docker compose run --rm osrm osrm-customize /data/zimbabwe-latest.osrm
```

Notes on each step:

- **`osrm-extract`** reads the `.osm.pbf` and applies a *profile* — a Lua script
  deciding which ways are routable and how fast they are. `/opt/car.lua` ships
  inside the image. Car is the right profile for kombis: they are minibuses on
  ordinary roads, so they obey car turn restrictions and one-ways. `foot` or
  `bicycle` would happily match a trace onto a footpath.
- **`osrm-partition`** and **`osrm-customize`** are the two MLD (multi-level
  Dijkstra) preprocessing passes. MLD is used rather than CH (`osrm-contract`)
  because it is far quicker to re-run when the extract is refreshed, and CH's
  faster queries do not matter at the volumes here. Whichever you pick, the
  algorithm must match on all three of `osrm-partition`/`osrm-customize` (or
  `osrm-contract`), `osrm-routed --algorithm`, and nothing else — the compose
  file says `--algorithm mld`.

The path passed to `osrm-partition` and `osrm-customize` is
`zimbabwe-latest.osrm`, not the `.osm.pbf`. That name refers to the whole family
of `zimbabwe-latest.osrm.*` files `osrm-extract` wrote next to the input; there
is no single file with that exact name, which is confusing the first time.

Expect roughly 1 GB in `./data` when all three steps have run.

### 3. Start the server

```bash
docker compose up -d osrm
docker compose logs -f osrm     # "running and waiting for requests" when ready
```

## Verifying

Two points a few hundred metres apart on Samora Machel Avenue:

```bash
curl "http://localhost:5000/match/v1/driving/31.0492,-17.8277;31.0530,-17.8281?geometries=geojson&overview=full"
```

A healthy response has `"code":"Ok"`, a `matchings` array with a `confidence`,
and one entry in `tracepoints` per input coordinate. `{"code":"NoMatch"}` on
*this* request means the dataset is not what you think it is; `NoMatch` on a
real trace is normal and handled — see below.

The pipeline reaches OSRM at `http://localhost:5000` by default; override with
`OSRM_URL` in the environment or `pipeline/.env`.

## Refreshing the extract

Geofabrik rebuilds nightly. To pick up new roads, re-download and re-run all
three preparation steps — `osrm-partition` and `osrm-customize` alone are not
enough, because the graph itself changed:

```bash
docker compose stop osrm
curl -L -o data/zimbabwe-latest.osm.pbf \
  https://download.geofabrik.de/africa/zimbabwe-latest.osm.pbf
docker compose run --rm osrm osrm-extract -p /opt/car.lua /data/zimbabwe-latest.osm.pbf
docker compose run --rm osrm osrm-partition /data/zimbabwe-latest.osrm
docker compose run --rm osrm osrm-customize /data/zimbabwe-latest.osrm
docker compose up -d osrm
```

Re-running the pipeline over already-matched traces after a refresh is
worthwhile: informal routes often use roads that OSM only recently acquired.

## Troubleshooting

| Symptom | Cause |
| ------- | ----- |
| `osrm` exits immediately, logs `Required files are missing` | The preparation steps have not been run, or wrote to a different `./data`. |
| `Invalid fingerprint` / `incompatible file version` | The dataset was prepared with a different OSRM version than the image. Re-run all three steps. |
| `Could not find any metrics for MLD` | Prepared with `osrm-contract` but served with `--algorithm mld`, or `osrm-customize` was skipped. |
| `{"code":"TooBig","message":"Too many trace coordinates"}` | More than `--max-matching-size` coordinates in one request. The pipeline chunks to 100; if you raised the flag, raise `MAX_COORDINATES_PER_REQUEST` in [`../pipeline/match.py`](../pipeline/match.py) to match. |
| `{"code":"NoMatch"}` on real traces | Usually genuine: the kombi used a road OSM does not have. `match_trace` logs it and falls back to the cleaned geometry for that stretch. Widen `radiuses` before assuming a broken dataset. |
| Port 5000 already bound (macOS) | AirPlay Receiver listens on 5000. Turn it off, or remap to `"5050:5000"` and set `OSRM_URL=http://localhost:5050`. |
| `osrm-extract` killed | Out of memory. It wants ~2 GB for Zimbabwe; raise Docker Desktop's memory limit. |

## Database

Postgres comes up with database `kombi` (user/pass `postgres`/`postgres`) on a
named volume, so data survives `docker compose down`. Use `down -v` to wipe it.

The API test suite wants a separate database:

```bash
docker compose exec postgis createdb -U postgres kombi_test
```

Schema comes from Alembic, not from this compose file — see
[`../api/README.md`](../api/README.md).
