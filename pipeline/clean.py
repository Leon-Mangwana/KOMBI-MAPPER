"""Trace cleaning: load raw GPS points, reject bad fixes, Kalman-smooth the rest.

Everything here works in **EPSG:32736** (UTM 36S) so that distances are metres
and speeds are m/s. Points are stored as EPSG:4326 and reprojected on load;
nothing in this module computes a distance in degrees. See ../CLAUDE.md.

The cleaning pass is three stages, in this order:

1. `drop_inaccurate` — throw away fixes the device itself flagged as poor.
2. `drop_speed_outliers` — throw away fixes that imply an impossible speed.
3. `kalman_smooth` — fit a constant-velocity model to what survives.

Order matters: the Kalman filter weights each measurement by its reported
accuracy, but a 200 m outlier with an optimistic accuracy would still drag the
track, so the gross errors are removed by the gates first.
"""

from __future__ import annotations

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point
from sqlalchemy import Engine, text

from config import get_engine

WGS84 = "EPSG:4326"
UTM36S = "EPSG:32736"

#: Fixes with a reported horizontal accuracy worse than this are discarded.
MAX_ACCURACY_M = 50.0

#: A step implying a speed above this is a GPS jump, not a kombi.
MAX_SPEED_KMH = 120.0

#: Floor on the measurement standard deviation, so R stays well conditioned
#: when a device reports an implausibly confident fix.
MIN_ACCURACY_M = 1.0

_TRACE_POINTS_SQL = text(
    """
    SELECT seq,
           ST_X(geom) AS lon,
           ST_Y(geom) AS lat,
           recorded_at,
           accuracy_m,
           speed_mps
    FROM trace_points
    WHERE trace_id = :trace_id
    ORDER BY seq
    """
)


def load_trace(trace_id: int, engine: Engine | None = None) -> gpd.GeoDataFrame:
    """Load one trace's raw points, reprojected to EPSG:32736.

    Returns a GeoDataFrame ordered by `seq` with columns `seq`, `recorded_at`,
    `accuracy_m`, `speed_mps` and a point `geometry`. Raises `LookupError` if
    the trace does not exist or has no points.
    """
    engine = engine if engine is not None else get_engine()
    with engine.connect() as conn:
        frame = pd.read_sql(_TRACE_POINTS_SQL, conn, params={"trace_id": trace_id})

    if frame.empty:
        raise LookupError(f"Trace {trace_id} has no points.")

    gdf = gpd.GeoDataFrame(
        frame.drop(columns=["lon", "lat"]),
        geometry=gpd.points_from_xy(frame["lon"], frame["lat"]),
        crs=WGS84,
    )
    return gdf.to_crs(UTM36S).reset_index(drop=True)


def trace_seconds(gdf: gpd.GeoDataFrame) -> np.ndarray:
    """Return `recorded_at` as float seconds elapsed since the first point.

    Working in relative seconds keeps the filter arithmetic in plain floats and
    sidesteps timezone handling in the inner loop.
    """
    if "recorded_at" not in gdf.columns:
        raise ValueError("Trace has no 'recorded_at' column; cannot derive speeds.")

    stamps = pd.to_datetime(pd.Series(gdf["recorded_at"]).reset_index(drop=True), utc=True)
    if stamps.isna().any():
        raise ValueError("Trace has points with no timestamp; cannot derive speeds.")
    if stamps.empty:
        return np.empty(0, dtype=float)

    return (stamps - stamps.iloc[0]).dt.total_seconds().to_numpy(dtype=float)


def drop_inaccurate(
    gdf: gpd.GeoDataFrame, max_accuracy_m: float = MAX_ACCURACY_M
) -> gpd.GeoDataFrame:
    """Drop fixes whose reported horizontal accuracy is worse than the limit.

    Points with no reported accuracy are kept: an unknown accuracy is not
    evidence of a bad fix, and the Kalman filter falls back to a default
    variance for them.
    """
    if "accuracy_m" not in gdf.columns:
        return gdf.copy()

    # Negated `>` rather than `<=` so that NaN (unknown accuracy) is kept.
    return gdf.loc[~(gdf["accuracy_m"] > max_accuracy_m)].copy()


def drop_speed_outliers(
    gdf: gpd.GeoDataFrame, max_speed_kmh: float = MAX_SPEED_KMH
) -> gpd.GeoDataFrame:
    """Drop fixes that imply an impossible speed from the previous kept fix.

    The comparison anchor is the last *retained* point, not the immediately
    preceding row. A single wild fix therefore costs one point rather than
    derailing the rest of the trace: the anchor stays put, the elapsed time to
    the next candidate grows, and the implied speed falls back under the limit.

    Two consequences worth knowing:

    - The first point is always kept, and is trusted as the initial anchor. A
      trace that opens on a wild fix will lose the points that follow it, which
      is why `drop_inaccurate` runs first — a cold-start fix nearly always
      carries a poor reported accuracy.
    - Points that do not advance the clock (duplicate or backwards timestamps)
      are dropped: they yield no usable speed and are redundant samples.
    """
    if len(gdf) <= 1:
        return gdf.copy()

    max_speed_mps = max_speed_kmh / 3.6
    xs = gdf.geometry.x.to_numpy(dtype=float)
    ys = gdf.geometry.y.to_numpy(dtype=float)
    seconds = trace_seconds(gdf)

    keep = np.zeros(len(gdf), dtype=bool)
    keep[0] = True
    anchor = 0

    for i in range(1, len(gdf)):
        elapsed = seconds[i] - seconds[anchor]
        if elapsed <= 0:
            continue
        distance = float(np.hypot(xs[i] - xs[anchor], ys[i] - ys[anchor]))
        if distance / elapsed <= max_speed_mps:
            keep[i] = True
            anchor = i

    return gdf.loc[keep].copy()


def kalman_smooth(
    gdf: gpd.GeoDataFrame,
    *,
    accel_noise_mps2: float = 1.0,
    default_accuracy_m: float = 10.0,
    initial_speed_sigma_mps: float = 15.0,
    rts_smooth: bool = True,
) -> gpd.GeoDataFrame:
    """Smooth a trace with a constant-velocity Kalman filter on (x, y, vx, vy).

    Implemented directly with NumPy — no filtering library. The model is the
    textbook continuous white-noise-acceleration one: position advances at the
    current velocity, and unmodelled acceleration enters as process noise.

        state  x = [x, y, vx, vy]ᵀ
        F      = [[1, 0, dt, 0], [0, 1, 0, dt], [0, 0, 1, 0], [0, 0, 0, 1]]
        Q      = σ_a² · [[dt³/3, 0, dt²/2, 0], [0, dt³/3, 0, dt²/2],
                         [dt²/2, 0,    dt, 0], [0, dt²/2, 0,    dt]]
        H      = [[1, 0, 0, 0], [0, 1, 0, 0]]
        R      = diag(accuracy_m², accuracy_m²)

    Per-point measurement noise comes from the device's reported `accuracy_m`
    (treated as a 1σ horizontal radius, which is what Android reports), so poor
    fixes are down-weighted rather than trusted equally. Points with no
    reported accuracy fall back to `default_accuracy_m`.

    `dt` is taken from `recorded_at` per step, so irregular sampling — which is
    normal on a phone that throttles GPS in the background — is handled exactly
    rather than assumed away.

    With `rts_smooth` (the default) a Rauch–Tung–Striebel backward pass runs
    after the forward filter. The forward pass alone only ever sees the past, so
    it lags every turn and stop; the backward pass conditions each estimate on
    the whole trace. That matters directly for `stops.detect_dwells`, which
    needs a standing vehicle to actually look stationary.

    Args:
        accel_noise_mps2: 1σ of unmodelled acceleration. Larger tracks harder
            manoeuvres and trusts the measurements more; smaller smooths more.
            The default suits a minibus in traffic.
        default_accuracy_m: Measurement 1σ for points with no reported accuracy.
        initial_speed_sigma_mps: 1σ of the initial velocity, which is seeded at
            zero and unknown.
        rts_smooth: Run the backward smoothing pass.

    Returns:
        A copy of `gdf` with `geometry` replaced by the smoothed positions and
        three columns added: `vx_mps`, `vy_mps`, `speed_kf_mps`. The device's
        own `speed_mps` column, if present, is left untouched.
    """
    count = len(gdf)
    if count == 0:
        return gdf.copy()

    measurements = np.column_stack(
        [gdf.geometry.x.to_numpy(dtype=float), gdf.geometry.y.to_numpy(dtype=float)]
    )
    seconds = trace_seconds(gdf)

    if "accuracy_m" in gdf.columns:
        accuracy = pd.Series(gdf["accuracy_m"]).to_numpy(dtype=float)
        accuracy = np.where(np.isnan(accuracy), default_accuracy_m, accuracy)
    else:
        accuracy = np.full(count, default_accuracy_m, dtype=float)
    accuracy = np.maximum(accuracy, MIN_ACCURACY_M)

    identity = np.eye(4)
    observation = np.zeros((2, 4))
    observation[0, 0] = 1.0
    observation[1, 1] = 1.0

    # Forward pass. The predicted moments and per-step F are kept because the
    # RTS pass needs them; at ~1 Hz a trace is thousands of points, so the
    # memory is trivial next to the clarity.
    x_pred = np.zeros((count, 4))
    p_pred = np.zeros((count, 4, 4))
    x_filt = np.zeros((count, 4))
    p_filt = np.zeros((count, 4, 4))
    transitions = np.zeros((count, 4, 4))

    x_filt[0] = [measurements[0, 0], measurements[0, 1], 0.0, 0.0]
    p_filt[0] = np.diag(
        [accuracy[0] ** 2, accuracy[0] ** 2, initial_speed_sigma_mps**2, initial_speed_sigma_mps**2]
    )
    x_pred[0] = x_filt[0]
    p_pred[0] = p_filt[0]
    transitions[0] = identity

    for k in range(1, count):
        dt = float(seconds[k] - seconds[k - 1])
        if dt <= 0.0:
            # drop_speed_outliers normally removes these; stay defined anyway.
            dt = 1e-3

        transition = identity.copy()
        transition[0, 2] = dt
        transition[1, 3] = dt
        transitions[k] = transition

        process_noise = _process_noise(dt, accel_noise_mps2)

        predicted_state = transition @ x_filt[k - 1]
        predicted_cov = transition @ p_filt[k - 1] @ transition.T + process_noise
        x_pred[k] = predicted_state
        p_pred[k] = predicted_cov

        measurement_cov = np.diag([accuracy[k] ** 2, accuracy[k] ** 2])
        innovation = measurements[k] - observation @ predicted_state
        innovation_cov = observation @ predicted_cov @ observation.T + measurement_cov

        # gain = P Hᵀ S⁻¹, solved rather than inverted; S is symmetric so
        # gainᵀ = S⁻¹ H P.
        gain = np.linalg.solve(innovation_cov, observation @ predicted_cov).T

        x_filt[k] = predicted_state + gain @ innovation
        # Joseph form: stays positive-definite even when the gain is imperfect.
        spread = identity - gain @ observation
        p_filt[k] = spread @ predicted_cov @ spread.T + gain @ measurement_cov @ gain.T

    states = x_filt
    if rts_smooth and count > 1:
        states = _rts_pass(x_filt, p_filt, x_pred, p_pred, transitions)

    smoothed = gdf.copy()
    smoothed["vx_mps"] = states[:, 2]
    smoothed["vy_mps"] = states[:, 3]
    smoothed["speed_kf_mps"] = np.hypot(states[:, 2], states[:, 3])
    smoothed = smoothed.set_geometry(
        gpd.GeoSeries(
            [Point(px, py) for px, py in states[:, :2]],
            index=smoothed.index,
            crs=gdf.crs,
        )
    )
    return smoothed


def _process_noise(dt: float, accel_noise_mps2: float) -> np.ndarray:
    """Q for a constant-velocity model driven by white-noise acceleration."""
    variance = accel_noise_mps2**2
    pos = dt**3 / 3.0
    cross = dt**2 / 2.0
    process_noise = np.zeros((4, 4))
    process_noise[0, 0] = process_noise[1, 1] = pos
    process_noise[2, 2] = process_noise[3, 3] = dt
    process_noise[0, 2] = process_noise[2, 0] = cross
    process_noise[1, 3] = process_noise[3, 1] = cross
    return process_noise * variance


def _rts_pass(
    x_filt: np.ndarray,
    p_filt: np.ndarray,
    x_pred: np.ndarray,
    p_pred: np.ndarray,
    transitions: np.ndarray,
) -> np.ndarray:
    """Rauch–Tung–Striebel backward recursion over the forward-pass moments."""
    count = len(x_filt)
    x_smooth = x_filt.copy()
    p_smooth = p_filt.copy()

    for k in range(count - 2, -1, -1):
        # C = P_filt Fᵀ P_pred⁻¹, solved via Cᵀ = P_pred⁻¹ F P_filt
        # (P_pred is symmetric).
        gain = np.linalg.solve(p_pred[k + 1], transitions[k + 1] @ p_filt[k]).T
        x_smooth[k] = x_filt[k] + gain @ (x_smooth[k + 1] - x_pred[k + 1])
        p_smooth[k] = p_filt[k] + gain @ (p_smooth[k + 1] - p_pred[k + 1]) @ gain.T

    return x_smooth


def clean_trace(
    gdf: gpd.GeoDataFrame,
    *,
    max_accuracy_m: float = MAX_ACCURACY_M,
    max_speed_kmh: float = MAX_SPEED_KMH,
    **kalman_kwargs: float | bool,
) -> gpd.GeoDataFrame:
    """Run the full cleaning pass and return the smoothed trace.

    Extra keyword arguments are forwarded to `kalman_smooth`. The result is
    re-indexed from zero; the original point ordering survives in `seq`.
    """
    cleaned = drop_inaccurate(gdf, max_accuracy_m=max_accuracy_m)
    cleaned = drop_speed_outliers(cleaned, max_speed_kmh=max_speed_kmh)
    cleaned = cleaned.reset_index(drop=True)
    return kalman_smooth(cleaned, **kalman_kwargs)  # type: ignore[arg-type]
