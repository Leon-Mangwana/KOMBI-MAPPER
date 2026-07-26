# Kombi Pipeline

Turns raw uploaded GPS traces into canonical routes and stops. This is stage 3
of the pipeline in [`../CLAUDE.md`](../CLAUDE.md); map matching, clustering and
GTFS export are still to come.

Stack: NumPy · GeoPandas · Shapely · SQLAlchemy.

## Setup

```bash
cd pipeline
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

The database URL defaults to the same one the API uses; override with
`DATABASE_URL` in the environment or a `.env` file.

## Modules

| Module         | What it does                                          |
| -------------- | ----------------------------------------------------- |
| `clean.py`     | Load a trace, reject bad fixes, Kalman-smooth          |
| `stops.py`     | Detect dwell events — where a kombi stood still        |
| `synthetic.py` | Generate traces with known ground truth, for tests     |
| `plot_trace.py`| Render raw vs cleaned for visual inspection            |

```python
from clean import clean_trace, load_trace
from stops import detect_dwells

cleaned = clean_trace(load_trace(42))
for dwell in detect_dwells(cleaned):
    print(dwell.start_time, f"{dwell.duration_s:.0f}s", dwell.centroid)
```

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

## Tests

No database needed — the tests build synthetic traces with injected noise,
dwells and outliers, and assert against that known truth:

```bash
pytest
```

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
- The pipeline reads the API's tables directly rather than importing the `api`
  package — they are separate installables, and only `trace_points` is needed.
