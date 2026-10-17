# Kombi Pipeline

Turns raw uploaded GPS traces into canonical routes and stops, and exports them
as a GTFS feed. This is stages 3 and 4 of the pipeline in
[`../CLAUDE.md`](../CLAUDE.md).

Stack: NumPy · GeoPandas · Shapely · scikit-learn · SQLAlchemy · httpx (OSRM).

## Setup

```bash
cd pipeline
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env
```

The database URL defaults to the same one the API uses; override with
`DATABASE_URL` in `.env` (every setting is documented in
[`.env.example`](.env.example)). Map matching needs an OSRM backend at
`OSRM_URL` (default `http://localhost:5000`) — `../infra/build-osrm.sh` prepares
and starts one; see [`../infra/README.md`](../infra/README.md).

## Modules

| Module         | What it does                                          |
| -------------- | ----------------------------------------------------- |
| `clean.py`     | Load a trace, reject bad fixes, Kalman-smooth          |
| `stops.py`     | Detect dwell events — where a kombi stood still        |
| `match.py`     | Map-match a cleaned trace onto roads via OSRM          |
| `process.py`   | The batch job: walk the `traces` table through the above |
| `frechet.py`   | Discrete Fréchet distance between two polylines        |
| `cluster.py`   | Cluster traces into routes and dwells into stops       |
| `naming.py`    | Carry route and stop names across a re-clustering run  |
| `gtfs.py`      | Export the clustered network as a zipped GTFS feed     |
| `synthetic.py` | Generate traces with known ground truth, for tests     |
| `seed_demo.py` | Upload synthetic traces, to exercise the system end to end |
| `plot_trace.py`| Render raw vs cleaned for visual inspection            |

```python
from clean import clean_trace, load_trace
from match import match_trace, store_matched_geom
from stops import detect_dwells

cleaned = clean_trace(load_trace(42))

for dwell in detect_dwells(cleaned):
    print(dwell.start_time, f"{dwell.duration_s:.0f}s", dwell.centroid)

matched = match_trace(cleaned)
print(f"{matched.matched_fraction:.0%} matched over {matched.length_m:.0f} m")
store_matched_geom(42, matched)
```

`match.match_stored_trace(42)` does all three steps in one call. Once every
trace is matched, clustering runs over the whole batch at once:

```bash
python cluster.py --dry-run   # cluster and report, write nothing
python cluster.py --replace   # rebuild routes, stops and route_stops
```

and the clustered network is exported as a GTFS feed:

```bash
python gtfs.py                # writes gtfs_out/gtfs.zip
```

When the API is deployed somewhere the pipeline is not, it cannot serve a file
this machine wrote. Export into the web app's static folder instead, and commit
it — see [`../web/README.md`](../web/README.md):

```bash
python gtfs.py -o ../web/public/gtfs.zip
```

In practice none of that is run by hand — `process.py` is the job that does it:

```bash
python process.py             # match every trace uploaded since the last run
python process.py --full      # ...then rebuild the network and re-export the feed
```

### Processing

The modules above are libraries: each takes a frame and gives one back, and none
of them decides *which* traces to work on. `process.py` is what walks the
`traces` table and turns them into a stage that runs.

```bash
python process.py --dry-run           # clean and match, write nothing
python process.py --limit 5           # a cautious first batch
python process.py --trace 42          # one trace, whatever its status
python process.py --retry             # include traces an earlier run rejected
python process.py --all               # every trace in the table
python process.py --full              # match, then rebuild the whole network
python process.py --full --recluster  # rebuild even if nothing new matched
```

**Statuses.** The API writes `uploaded`; this stage writes one of three terminal
states, and `process.py` picks up whatever is still `uploaded`:

| Status     | What it means                                                     |
| ---------- | ----------------------------------------------------------------- |
| `matched`  | Cleaned, matched, `matched_geom` stored. Clustering will see it.   |
| `rejected` | Ran fine, but too little of the trace matched to trust it.         |
| `failed`   | Could not be processed: no points, no timestamps, too few fixes.   |

**The quality gate.** `match_trace` never fails a trace outright — a stretch
OSRM cannot match falls back to cleaned GPS geometry and is flagged on the
result. That is right for a library and wrong to feed to clustering unchecked: a
trace that came back 30% matched is mostly GPS noise, and the Fréchet distances
it produces would be meaningless. So a trace whose matched fraction is under
`MIN_MATCHED_FRACTION` (0.6, `--min-matched-fraction`) is `rejected` and its
geometry is *not* stored — `matched_geom` is the only signal clustering reads,
so storing it and hoping would let the trace back in. A re-run that rejects a
trace an earlier run had stored clears the old geometry for the same reason.

The default is deliberately lenient. Harare's network runs partly on roads OSM
has thinly mapped, and a trace genuinely on a road OSRM does not know about
falls back through no fault of the recording. The gate is there to catch traces
that are *mostly* fallback — a walk, a private car through a compound, a phone
that spent the trip in a bag.

**OSRM being down is not a bad trace.** An `OSRMError` means the matcher was
unreachable or answered nonsense, which says nothing about the trace in hand.
Marking traces `failed` on it would burn through the backlog in seconds and
leave a table of perfectly good traces this stage would never look at again. So
it aborts the run instead: everything already processed keeps its status,
everything untouched stays `uploaded`, and a re-run after OSRM is back picks up
where it stopped. `--full` will not cluster after an aborted run — a network
built from an arbitrary prefix of the backlog is worse than yesterday's.

**`--full`** runs clustering and the GTFS export after matching. Clustering is
global, so it runs once at the end rather than per trace, and it is skipped
entirely when the run matched nothing new — re-clustering identical inputs would
spend minutes writing the same routes back. `--recluster` overrides that skip,
for after a threshold has been tuned. The new network always *replaces* the old
one; names survive through [naming](#naming). (`--replace` is accepted as the
old spelling of `--recluster`, so existing cron lines keep working.)

**Running it on a schedule.** Nothing triggers this stage: the API only stores
uploads, and the deployed API has no OSRM to call. The pipeline runs on an
operator's machine, where OSRM and this checkout are, so the schedule lives
there too. `process.py --full` is idempotent — nothing new means nothing
rewritten, and a run that OSRM interrupts leaves the backlog `uploaded` for the
next one — so it is safe to run as often as you like. Nightly is plenty:

```bash
# cron (Linux/macOS): 02:00 daily, with OSRM up
0 2 * * * cd /path/to/pipeline && .venv/bin/python process.py --full >> process.log 2>&1
```

```powershell
# Windows Task Scheduler: a daily task running this action
schtasks /Create /SC DAILY /ST 02:00 /TN "Kombi pipeline" `
  /TR "cmd /c cd /d C:\path\to\pipeline && .venv\Scripts\python.exe process.py --full >> process.log 2>&1"
```

When the API is deployed apart from the pipeline, the exported feed still has
to reach it — see [GTFS export](#gtfs-export) for the `web/public/gtfs.zip`
hand-off, which a scheduled run cannot do for you.

### Cleaning

`load_trace` reads `trace_points` and reprojects to EPSG:32736, so everything
downstream is in metres. `clean_trace` then runs three stages in order:

1. **Accuracy gate** — drop fixes reporting worse than 50 m. Points with no
   reported accuracy are kept; an unknown accuracy is not evidence of a bad fix.
2. **Speed gate** — drop fixes implying over 120 km/h from the last *retained*
   point. Anchoring on the last kept point rather than the previous row means a
   single wild fix costs one point instead of derailing the rest of the trace.
3. **Kalman filter** — a constant-velocity model on `(x, y, vx, vy)`, written
   directly with NumPy. Per-point measurement noise comes from the device's
   reported `accuracy_m`, `dt` from consecutive timestamps, and a
   Rauch–Tung–Striebel backward pass follows the forward filter.

The gates run before the filter because a 200 m outlier reporting an optimistic
accuracy would otherwise drag the track.

The backward pass is what makes dwell detection work. A forward-only filter
never sees past the current point, so it lags every stop; conditioned on the
whole trace instead, a standing vehicle actually looks stationary.

### Dwell detection

`detect_dwells` reports runs of at least 20 s where speed stays under 1.0 m/s,
returning a centroid, start time and duration for each. Speed is measured per
*segment* rather than per point, which keeps the reported duration exact.

Two adjacent runs are merged when the stretch between them is both shorter than
`max_gap_s` and covers less than `max_gap_m` of ground. This matters: even after
smoothing, a parked vehicle throws the occasional 1–2 m/s blip, and under a
strictly literal reading one blip shatters a 45 s stop into fragments that each
fall under the 20 s minimum. The displacement condition is what makes the merge
safe — a noise blip goes nowhere, whereas a kombi that actually pulled away is
metres down the road. Pass `max_gap_s=0` for the strict reading.

### Map matching

A cleaned trace is smooth but still floats beside the road — a Kalman filter
knows nothing about where the tarmac is. `match_trace` snaps it onto the road
network with OSRM's `/match` service and writes the result to
`traces.matched_geom` as a single EPSG:4326 LineString. Single, not multipart:
the column is typed `LINESTRING` and PostGIS rejects a MULTILINESTRING.

**Chunking.** `osrm-routed` caps a request at `--max-matching-size` coordinates
(100 by default), so a 40-minute trace at 1 Hz is split across requests. Chunks
overlap by 10 points and each keeps only the half of the overlap where it had
road context on both sides; consecutive chunks share the seam point itself, so
their matched pieces are cut at the same input point and meet there. Without the
overlap, every seam would be a boundary where the matcher was working blind.

Stitching is done by index, not by geometry: each response's `tracepoints` array
maps input point → snapped location one-for-one, so each piece is cut (in metres)
at the snapped locations of the first and last point its chunk owns. That mapping
is why `tidy` is never enabled — tidying drops input points and the mapping with
them.

**`NoMatch` is normal.** Kombis use unpaved shortcuts and roads OSM has not
caught up with, so a stretch with no plausible road under it is routine rather
than exceptional. Failing the trace would throw away everything that *did*
match, so the unmatched stretch falls back to the cleaned geometry, joined to
the matched pieces either side, logged, and reported on `MatchResult.segments`:

```python
for segment in matched.segments:
    if not segment.matched:
        print(f"points {segment.start_index}-{segment.end_index}: {segment.reason}")
```

`matched_fraction` is the number to gate clustering on. That tolerance covers
genuine match failures only (`NoMatch`, `NoSegment`) — a `TooBig`, a 5xx or a
connection error means a bug or an outage, not a missing road, and raises
`OSRMError` rather than quietly producing an unmatched trace.

Per-point `radiuses` come from the device's reported `accuracy_m` so a fix the
phone already knew was poor is matched loosely instead of being forced onto the
nearest road, and per-point `timestamps` are sent when they are strictly
increasing — the single biggest quality lever on a noisy urban trace.

Requests are `GET`, not `POST`: `osrm-routed` serves GET only, with coordinates
in the URL path. Chunking keeps those URLs to roughly 4 KB.

### Clustering

`cluster.py` is the stage that turns a pile of individual rides into a network.
Two clusterings run over different things and are then joined.

**Routes.** Matched traces are compared pairwise with the discrete Fréchet
distance and grouped with `DBSCAN(metric="precomputed", eps=200)`, in metres.
Each cluster is a route; its `canonical_geom` is the cluster **medoid**, the
member with the smallest total distance to the others.

Fréchet rather than Hausdorff because it respects *order*: two traces down the
same roads in opposite directions have a Hausdorff distance of nearly zero and
are not the same route. DBSCAN rather than k-means because the number of kombi
routes in Harare is the thing being measured, most uploads are one-offs that
belong in no cluster, and a "route" has no mean — the average of two traces down
parallel streets runs through the buildings between them. A medoid keeps the
canonical geometry a real trace that a real kombi really drove.

`eps` is a *maximum* over the coupling, not an average, and on two traces of one
route the maximum is almost always at the ends — contributors board where they
live, not at the terminus. 200 m is about two Harare blocks; the middle of the
route costs nothing, the ends set the number.

**Preparation matters more than it looks.** Traces are simplified with
Douglas–Peucker at 15 m *and then resampled to a uniform 50 m spacing*. The
second step is not an optimisation. The discrete Fréchet distance can only pair
a vertex with a vertex, and Douglas–Peucker keeps vertices only at bends — so a
3 km straight leg comes back with vertices at its ends and nothing between. When
a bend clears the tolerance on one trace but not the other, the leftover vertex
has no partner within kilometres and the pairing distance becomes the distance
to the far end of the leg. Two traces never more than 10 m apart then measure
365 m apart, which is what the first run of these tests actually did. No choice
of `eps` fixes it, because the error scales with the longest straight leg.
Resampling bounds the artefact at half a step.

**Stops.** Every dwell `stops.detect_dwells` found on any clustered trace goes
into one pile and is clustered with `DBSCAN(metric="euclidean", eps=40)` on the
projected coordinates — dwells are points, and points do have a mean, so a stop
sits at the mean of its dwells. A cluster survives only with **three distinct
traces** behind it. Distinct traces, not dwell events: three dwells on one ride
is one kombi in one stop-start traffic queue, which is one observation rather
than three.

**Joining the two.** A stop belongs to a route when one of the route's traces
dwelled there, and a route's stops are ordered by `shapely.line_locate_point` —
the distance along `canonical_geom` of the nearest point on it. That is the
ordering as the kombi drives it, and it is the only one that survives a route
doubling back: on an out-and-back route a stop 200 m from the start on the
return leg is the *last* stop, which ordering by distance from the origin, or by
chaining nearest neighbours, gets backwards.

Routes, stops and `route_stops` are written in one transaction, and each
clustered trace gets its `route_id` set. They are derived tables, so a rerun
replaces them: `process.py --full` always does, and `cluster.py` on its own
wants `--replace` (without it a second network is written beside the first,
which is only ever useful for comparing two clusterings side by side). The
hand-entered `routes.short_name` and `stops.name` values survive the rebuild —
see the next section.

### Naming

Clustering cannot name anything — a cluster of GPS traces has no idea it is the
Mbare route — so `routes.short_name`, `routes.long_name` and `stops.name` are
filled in by a person, through `PATCH /routes/{id}` or the web app's operator
mode. That makes them the most expensive data in the system: the only part
nobody can recompute.

Re-clustering used to destroy it. `--replace` deleted the derived tables and
rebuilt them, which is right for geometry and wrong for names; running without
it added a second set of routes beside the first. The operator's choice was
duplicates or amnesia, and `process.py --full` defaulted to duplicates.

`naming.py` is the third option. Before the delete, it reads every named row and
matches it against the new clustering:

- **Routes** by discrete Fréchet distance, on geometries resampled exactly as
  `cluster_routes` resamples them, with `ROUTE_EPS_M` as the threshold — the
  same measure that decided two traces were one route decides a new route is the
  old one. A corridor that is still the same corridor moves by metres. One that
  has genuinely changed exceeds the threshold and arrives unnamed, which is
  correct: somebody should look at it.
- **Stops** by plain distance between centroids, within `STOP_EPS_M`.

Matching is direction-sensitive, deliberately: inbound and outbound along one
road are separate routes with separate names, and matching a reversed geometry
would hand each the other's. It is also one-to-one — two new routes resembling
one old name cannot both claim it, so the closer takes it and the other is left
for review. Names that match nothing are logged as a warning, because a name
with no home means a route has changed shape or stopped being detected.

`--forget-names` opts out and rebuilds the names away.

### GTFS export

`gtfs.py` writes the eight files a consumer needs — `agency.txt`, `stops.txt`,
`routes.txt`, `trips.txt`, `stop_times.txt`, `shapes.txt`, `calendar.txt` and
`frequencies.txt` — zipped at the archive root.

Most of it is a translation of rows the clustering already decided. One question
is genuinely hard, and it is the shape of the whole file:

**Kombis have no timetable.** A kombi leaves the rank when it is full. No
departure time is published, none is adhered to, and no amount of trace
collection will produce one, because there is nothing there to observe. GTFS has
a facility for exactly this and it is `frequencies.txt`: a single *reference
trip* in `stop_times.txt` giving the pattern — which stops, in what order, how
far apart — and then, per time band, a headway for how often that pattern
repeats. `exact_times=0` says "roughly every N minutes, no guaranteed clock
times", which is what a kombi service is.

The alternative is synthesising a `trips.txt` row per notional departure at
06:00, 06:15, 06:30. That produces a feed of exactly the shape a trip planner
renders as a timetable, promising a passenger a 06:15 departure that nobody has
committed to and that no observation supports. Every number in it would be
invented here. So: frequencies, and the headways come from departures that
actually happened.

**Headways.** Every clustered trace is one observed departure, timestamped at
its first point. Departures are converted to the agency timezone, bucketed into
five bands of the operating day, grouped by date, and differenced:

| Band            | Local time  |
| --------------- | ----------- |
| `early_morning` | 04:00–06:00 |
| `am_peak`       | 06:00–09:00 |
| `midday`        | 09:00–15:00 |
| `pm_peak`       | 15:00–19:00 |
| `evening`       | 19:00–22:00 |

The band's headway is the **median** of those gaps, rounded to the minute. Both
groupings matter: the interval from the last kombi on Tuesday to the first on
Wednesday is an overnight closure rather than a headway, and the interval from
08:00 to 14:00 spans a peak and a lull and describes neither.

**The bias is real, large, and one-sided.** Contributors ride some kombis and
not others, so an observed gap spans every departure nobody recorded. The
estimate is an *upper bound* on the true headway, tightening as coverage
improves. It is still a measurement of a real quantity converging on the right
answer, which is more than a fabricated timetable can claim — but it is why
headways are clamped to 2–60 minutes, and why a band is only published when a
departure was actually recorded in it. `python gtfs.py` prints the basis of
every headway it wrote (`band`, `route` or `default`) so that how much of a feed
is measured is visible at export time.

A route with no observed departure at all still gets one all-day row at the
default headway: a trip with no `frequencies.txt` entry is not a frequency-based
trip, it silently becomes a single scheduled departure at midnight. Pass
`--fill-bands` to publish every band for every route, filling the unobserved
ones from the route's own median — a more usable feed, a less honest one.

**Running times.** `stop_times.txt` still needs times, and they are what the
spec says they are for a frequency-based trip: offsets from the trip's own
start, not clock times. The reference trip is anchored at `00:00:00` and each
stop's offset is its distance along the route over the route's observed
operating speed — the median across its traces of matched length over wall-clock
duration. That already includes time spent stopped, so no separate dwell
allowance is added; adding one would count the same seconds twice. Every row
carries `timepoint=0`, which marks the times approximate.

**Everything else worth knowing.** `stops.name` and `routes.short_name` are the
columns clustering cannot fill in, so an unnamed stop is exported as
`Stop <id>`, and a route with no name at all is named after its terminals, or
failing that `R<id>`. Shapes are Douglas–Peucker simplified at 5 m — stop
distances are measured on the *simplified* line, so `shape_dist_traveled` agrees
between `shapes.txt` and `stop_times.txt`. A route with fewer than two stops is
excluded and logged: a one-stop trip is not a trip a passenger can take. The zip
is byte-identical when the network has not changed, so `sha256sum` answers "is
this new?".

```bash
python gtfs.py                       # writes gtfs_out/gtfs.zip
python gtfs.py -o /srv/kombi/gtfs.zip
python gtfs.py --fill-bands          # publish every band, observed or not
```

Agency details come from the environment — `GTFS_AGENCY_NAME`, `GTFS_AGENCY_URL`
and `GTFS_AGENCY_TIMEZONE` (default `Africa/Harare`, which is also the timezone
the time bands are read in). The defaults are valid GTFS placeholders; a real
deployment must set at least the name and URL. The API serves whatever
`gtfs.py` last wrote at `GET /gtfs.zip` — see
[`../api/README.md`](../api/README.md).

### Validating the feed

The feed is checked with [MobilityData's
gtfs-validator](https://github.com/MobilityData/gtfs-validator), which is the
reference implementation of the spec's rules and what a consumer will judge the
feed by. Download the latest release jar (it needs Java 17+) and run it over the
export:

```bash
python gtfs.py -o gtfs_out/gtfs.zip

java -jar gtfs-validator.jar \
  --input gtfs_out/gtfs.zip \
  --output_base gtfs_out/validation \
  --country_code ZW
```

That writes `report.html` (open it in a browser), `report.json` for scripting,
and `system_errors.json`. Notices come in three severities and only the first is
a failure:

- **ERROR** — the feed breaks the spec. Nothing here should ever produce one; if
  one appears, it is a bug in `gtfs.py`, not a data problem to be excused.
- **WARNING** — legal but likely wrong. Read every one.
- **INFO** — advisory.

To fail a build on it, `report.json` has a `notices` array with a `severity` on
each entry:

```bash
python -c "import json,sys; \
  n=json.load(open('gtfs_out/validation/report.json'))['notices']; \
  e=[x for x in n if x['severity']=='ERROR']; \
  print(f'{len(e)} error notice(s)'); sys.exit(1 if e else 0)"
```

There is also a [web validator](https://gtfs-validator.mobilitydata.org/) that
takes an uploaded zip, which is quicker for a one-off look and needs no Java.

Two things it will not tell you, because no validator can. First, whether the
headways are *true* — they are valid GTFS whatever they say, and the honest
check is the `basis` column `gtfs.py` prints. Second, whether the routes are
real; that is what the trace coverage and `ROUTE_MIN_TRACES` are for.

The `pytest` suite encodes the rules the validator enforces that the exporter
could plausibly break — strictly increasing stop times and shape distances, no
unnamed stop, no single-stop trip, no overlapping frequency rows — so a
regression should surface there before it reaches a jar.

## Tests

No database and no OSRM needed — the tests build synthetic traces with injected
noise, dwells and outliers, and assert against that known truth:

```bash
pytest
```

Map matching is tested against a fake OSRM (an `httpx.MockTransport` that snaps
coordinates onto the same polyline the traces are generated along), so the
correct matched output is known exactly: it is the road. That makes the stitching
assertions real ones — a seam that duplicated its overlap or dropped it would
show up as ~100 m of length error per chunk.

Measured over 40 seeds on a 400 s trace with 4 m per-axis noise and two injected
dwells: position RMSE 2.2 m against 5.7 m raw, both dwells found every time,
durations within 5 s of injected, centroids within 2 m.

The Fréchet implementation is checked two ways: against coupling matrices worked
out by hand, written into the test alongside the arithmetic that produced them,
and against a brute-force enumeration of every monotone coupling — the
definition itself, at exponential cost, so only on inputs of a few points.
Clustering is tested on traces built along known corridors, so the routes and
stops it *should* find are known by construction.

The GTFS tests build routes, stops and departure times directly, so every field
the feed should contain is known too. Departures are built in UTC, as the
database stores them, and asserted against Africa/Harare bands — a 05:30 UTC
departure belongs to the morning peak, and reading it as 05:30 local would file
it two bands away.

`process.py` is tested for its orchestration rather than its arithmetic — which
traces get picked up, which status each outcome earns, what is written and what
is deliberately not, and that an unreachable OSRM stops the run instead of
condemning the backlog. The cleaning and matching it drives are stubbed there,
since they have suites of their own.

Persistence is the part with no automated coverage: `cluster.persist_network`,
`gtfs.load_export_network` and `process.py`'s handful of statements are the
functions that need PostGIS, and the rest of the suite deliberately needs no
database.

## Seeding a demo network

To see the whole system work without riding a kombi first — or before preparing
an OSRM dataset, which is a multi-gigabyte one-off:

```bash
python seed_demo.py --fake-match    # 12 traces along 3 corridors, via POST /traces
python cluster.py --replace
python gtfs.py
```

That yields 3 routes of 4 traces each, 3 stops apiece, and a valid feed. Traces
go in **over HTTP**, so the API must be running and the real ingest path is
exercised. The corridors and dwell windows are known by construction, so the
output can be checked against what went in.

`--fake-match` writes each trace's *cleaned* geometry into `matched_geom` and
marks it `matched`. That is not map matching and the result is not a road — it
is a smoothed GPS track. It exists so clustering, the export and the web map can
be exercised before OSRM is up. Once it is, `python process.py --all` re-matches
the same traces properly.

## Visual inspection

Tests confirm tolerances; they do not show a cut corner or a lagged turn.
`plot_trace.py` renders the plan view, a zoom on the first dwell, and segment
speed against the dwell threshold:

```bash
python plot_trace.py --demo                      # synthetic, ground truth drawn in
python plot_trace.py --trace-id 42 --out t42.png # a real trace from the database
```

## Notes

- Geometry is stored as EPSG:4326 and reprojected to EPSG:32736 (UTM 36S) on
  load. Never compute distances in degrees. See [`../CLAUDE.md`](../CLAUDE.md).
  `match.py` is the one module that has to deal in EPSG:4326 mid-flight, because
  OSRM speaks lon/lat — it reprojects every response back to metres before
  measuring or cutting anything.
- The pipeline reads and writes the API's tables directly rather than importing
  the `api` package — they are separate installables, and only `trace_points`,
  `traces`, `routes`, `stops` and `route_stops` are needed.
- `frechet.py` implements the discrete Fréchet distance from scratch on NumPy
  arrays; there is no library dependency for it. The Eiter–Mannila recursion is
  filled bottom-up rather than memoised recursively, which computes the same
  table without a recursion depth of `n + m`, and keeps two rows rather than
  the whole `n × m` matrix.
