# Infrastructure

Local backing services for Kombi Route Mapper, via docker-compose.

| Service   | Image                       | Port | Purpose                                     |
| --------- | --------------------------- | ---- | ------------------------------------------- |
| `postgis` | `postgis/postgis:15-3.4`    | 5432 | Traces, routes and stops (the API's database) |
| `osrm`    | `osrm/osrm-backend:v5.25.0` | 5000 | Map matching for [`../pipeline/match.py`](../pipeline/match.py) |

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
format does not match its own. That is also why the image is pinned to a version
tag rather than `latest`: `latest` is rebuilt, and a rebuild would silently
invalidate the dataset in `./data`. `v5.25.0` is the newest versioned tag
`osrm/osrm-backend` publishes.

### 1. Download the Zimbabwe extract

Geofabrik publishes a daily country extract:

```bash
mkdir -p data
curl -L -o data/zimbabwe-latest.osm.pbf \
  https://download.geofabrik.de/africa/zimbabwe-latest.osm.pbf
```

About 180 MB as of July 2026. `data/` is gitignored — extracts and the `.osrm.*`
files built from them are large and reproducible, so they never get committed.

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
# Build the routing graph with the car profile. The slow step: budget several
# minutes and give Docker Desktop at least 6 GB of memory.
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
  preprocessing and `osrm-routed --algorithm` must agree. Note that
  `osrm-routed` defaults to `CH`, so serving an MLD dataset without passing
  `--algorithm mld` fails — the compose file passes it.

The path passed to `osrm-partition` and `osrm-customize` is
`zimbabwe-latest.osrm`, not the `.osm.pbf`. That name refers to the whole family
of `zimbabwe-latest.osrm.*` files `osrm-extract` wrote next to the input; there
is no single file with that exact name, which is confusing the first time.

Expect a few GB in `./data` once all three steps have run — the `.osrm.*` family
is several times the size of the `.osm.pbf` it came from.

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
| `osrm-extract` killed, or exits 137 | Out of memory. Raise Docker Desktop's memory limit to 6 GB or more. |

## Basemap tiles

Both front ends render on a blank background until they are pointed at a
basemap. Protomaps only — this project does not use Mapbox.

A [PMTiles](https://protomaps.com/) archive is a **single file** the browser
reads with HTTP range requests, so "hosting tiles" means putting one file
somewhere that serves ranges and allows CORS. There is no tile server to run,
which is why this section is much shorter than the OSRM one above.

### 1. Cut a Harare extract

Protomaps publishes a daily planet build. The `pmtiles` CLI can slice a bounding
box straight out of it over the network, downloading only the tiles inside the
box rather than the whole planet:

```bash
# Windows: grab pmtiles.exe from the same releases page.
# https://github.com/protomaps/go-pmtiles/releases

pmtiles extract https://build.protomaps.com/YYYYMMDD.pmtiles harare.pmtiles \
  --bbox=30.60,-18.20,31.45,-17.55 \
  --maxzoom=14
```

Use the most recent date listed at <https://build.protomaps.com/>.

The bounding box is Harare **plus Norton, Ruwa and Chitungwiza**, for the same
reason the OSRM extract is the whole country: kombi routes run out to all three,
and a route leaving the box would cross a hole in the map.

`--maxzoom=14` is enough to read a street layout; each further zoom level is
roughly four times the size, and 15 mainly adds building footprints. Expect tens
of megabytes at 14.

### 2. Put it somewhere with ranges and CORS

**Cloudflare R2** is the usual home: 10 GB free, no egress charges, and range
requests work out of the box.

1. Create a bucket and upload `harare.pmtiles`.
2. Attach a custom domain. The `r2.dev` public URL is rate-limited and
   explicitly not for production use.
3. Add a CORS policy, or the browser will refuse every range request:

```json
[
  {
    "AllowedOrigins": ["https://your-site.netlify.app"],
    "AllowedMethods": ["GET", "HEAD"],
    "AllowedHeaders": ["range", "if-match"],
    "ExposeHeaders": ["etag", "content-range", "content-length"],
    "MaxAgeSeconds": 3600
  }
]
```

`ExposeHeaders` matters as much as the origin — without `content-range` the
client cannot tell what it was given back.

For a small extract you can instead drop the file in `web/public/` and let
Netlify serve it from the site's own origin, which needs no CORS at all. The
trade is that a binary in git stays in the history forever and grows the clone
for everyone, so it is only reasonable for something in the low tens of
megabytes that you will not re-cut often.

### 3. Fonts, if you want labels

Map labels need glyph files, and a style asking for text without them renders
nothing while logging an error per tile. Self-host Protomaps' font assets rather
than hot-linking someone else's:

```bash
git clone --depth 1 https://github.com/protomaps/basemaps-assets
cp -r basemaps-assets/fonts ../web/public/fonts
```

Then set `VITE_GLYPHS_URL=/fonts/{fontstack}/{range}.pbf`. The braces are part of
the URL template — MapLibre fills them in.

Labels are worth having on the public map: "does this go to Highfield?" is the
question it exists to answer, and suburb names are how a reader finds Highfield.
The capture PWA deliberately stays label-free — it is a backdrop for a live
position on a phone, where labels are clutter and extra download.

### 4. Point the apps at it

Set these in each Netlify site's environment variables and redeploy — Vite bakes
them in at build time, so a redeploy is required, not a restart:

| Variable | Value |
| -------- | ----- |
| `VITE_PMTILES_URL` | `https://tiles.example.org/harare.pmtiles` |
| `VITE_GLYPHS_URL` | `/fonts/{fontstack}/{range}.pbf` (web only) |

Locally, put the same lines in `web/.env` and `capture/.env`.

To verify: the map should show land, water and roads instead of flat dark blue.
If it stays blank, the browser console will say which — a CORS rejection, a 404,
or a 403 from a bucket that is not public.

### Refreshing

Re-run the extract against a newer build and replace the file. Nothing else
changes; the URL stays the same and the apps need no redeploy. Worth doing after
a mapping push in Harare, since new roads appear in OSM first.

## Database

Postgres comes up with database `kombi` (user/pass `postgres`/`postgres`) on a
named volume, so data survives `docker compose down`. Use `down -v` to wipe it.

The API test suite wants a separate database:

```bash
docker compose exec postgis createdb -U postgres kombi_test
```

Schema comes from Alembic, not from this compose file — see
[`../api/README.md`](../api/README.md).
