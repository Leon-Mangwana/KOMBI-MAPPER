# Kombi Pipeline

Turns raw uploaded GPS traces into canonical routes and stops. This is stage 3
of the pipeline in [`../CLAUDE.md`](../CLAUDE.md); clustering and GTFS export are
still to come.

Stack: NumPy · GeoPandas · Shapely · SQLAlchemy · httpx (OSRM).

## Setup

```bash
cd pipeline
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

The database URL defaults to the same one the API uses; override with
`DATABASE_URL` in the environment or a `.env` file. Map matching needs an OSRM
backend at `OSRM_URL` (default `http://localhost:5000`) — see
[`../infra/README.md`](../infra/README.md) for preparing and running one.

## Modules

| Module         | What it does                                          |
| -------------- | ----------------------------------------------------- |
| `clean.py`     | Load a trace, reject bad fixes, Kalman-smooth          |
| `stops.py`     | Detect dwell events — where a kombi stood still        |
| `match.py`     | Map-match a cleaned trace onto roads via OSRM          |
| `synthetic.py` | Generate traces with known ground truth, for tests     |
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

`match.match_stored_trace(42)` does all three steps in one call.

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
  the `api` package — they are separate installables, and only `trace_points`
  and `traces.matched_geom` are needed.
