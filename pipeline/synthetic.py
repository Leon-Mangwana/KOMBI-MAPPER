"""Synthetic traces with known ground truth, for tests and visual inspection.

A generated trace carries both what the "phone" recorded and the noise-free
path it was recorded from, so tests can assert against truth instead of against
a previously-recorded output. Noise, dwell periods and outliers are all
injected deliberately and reported back on the result.

Everything is generated directly in EPSG:32736 (metres), which is the CRS the
cleaning functions work in — `load_trace` is the only thing that ever deals in
EPSG:4326.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import LineString, Point

from clean import UTM36S

#: Somewhere near central Harare, in UTM 36S. Only the neighbourhood matters —
#: metric calculations are translation-invariant — but a realistic origin keeps
#: reprojected output plausible if a trace is ever written back out.
DEFAULT_ORIGIN = (296_000.0, 8_026_000.0)

#: An L-shaped route: 3 km east, then 3 km north. The corner is the point of
#: it — a constant-velocity model is exactly right on a straight line, so a
#: straight-only path would flatter the filter.
DEFAULT_WAYPOINTS = ((0.0, 0.0), (3000.0, 0.0), (3000.0, 3000.0))

DEFAULT_START = pd.Timestamp("2026-07-24T08:00:00Z")


@dataclass(frozen=True)
class SyntheticTrace:
    """A generated trace and the ground truth it was generated from.

    Attributes:
        noisy: What the device "recorded" — the frame under test.
        truth: The same points without noise or outliers, same length and
            ordering, so errors are a row-wise comparison.
        dwells: The injected dwell windows as `(start_s, duration_s)`, relative
            to the first sample.
        dropped_indices: Positional indices of every injected outlier, i.e. the
            rows a correct cleaning pass is expected to reject.
    """

    noisy: gpd.GeoDataFrame
    truth: gpd.GeoDataFrame
    dwells: tuple[tuple[float, float], ...] = ()
    dropped_indices: tuple[int, ...] = field(default=())

    def true_xy(self) -> np.ndarray:
        return np.column_stack(
            [self.truth.geometry.x.to_numpy(dtype=float), self.truth.geometry.y.to_numpy(dtype=float)]
        )


def synthetic_trace(
    *,
    duration_s: float = 300.0,
    sample_hz: float = 1.0,
    speed_mps: float = 10.0,
    dwells: tuple[tuple[float, float], ...] = (),
    noise_sigma_m: float = 4.0,
    reported_accuracy_m: float | None = None,
    seed: int = 0,
    bad_accuracy_indices: tuple[int, ...] = (),
    bad_accuracy_m: float = 120.0,
    bad_accuracy_offset_m: float = 150.0,
    jump_indices: tuple[int, ...] = (),
    jump_offset_m: float = 4000.0,
    waypoints: tuple[tuple[float, float], ...] = DEFAULT_WAYPOINTS,
    origin: tuple[float, float] = DEFAULT_ORIGIN,
    start_time: pd.Timestamp = DEFAULT_START,
) -> SyntheticTrace:
    """Generate a trace that follows `waypoints`, pausing during `dwells`.

    The vehicle travels the polyline at a constant `speed_mps`, except inside a
    dwell window where it does not advance at all. Distance covered over each
    sampling interval is the interval's length minus its overlap with any dwell
    window, so a dwell that starts and ends on a sample boundary is reproduced
    exactly — which is what lets the dwell tests assert durations tightly.

    Args:
        duration_s: Length of the recording.
        sample_hz: Sampling rate. 1 Hz is what the capture PWA targets.
        speed_mps: Travel speed while not dwelling (10 m/s ≈ 36 km/h).
        dwells: Windows of `(start_s, duration_s)` to stand still for.
        noise_sigma_m: 1σ of the Gaussian noise added to each axis.
        reported_accuracy_m: What the device claims its accuracy is. Defaults
            to `noise_sigma_m`, i.e. an honest device.
        seed: Seeds the RNG, so a given call is reproducible.
        bad_accuracy_indices: Rows to turn into poor fixes — displaced by
            `bad_accuracy_offset_m` and honestly reporting `bad_accuracy_m`.
            The accuracy gate should catch these.
        jump_indices: Rows to displace by `jump_offset_m` while still reporting
            a *good* accuracy. Only the speed gate can catch these.
        waypoints: Path to follow, relative to `origin`, in metres.
        origin: UTM 36S easting/northing the waypoints are offset from.
        start_time: Timestamp of the first sample.

    Returns:
        A `SyntheticTrace` with the noisy frame, the truth frame, and the
        injected dwell windows and outlier indices.
    """
    if sample_hz <= 0:
        raise ValueError("sample_hz must be positive.")

    step = 1.0 / sample_hz
    seconds = np.arange(0.0, duration_s + step / 2, step)
    count = len(seconds)

    path = LineString([(origin[0] + wx, origin[1] + wy) for wx, wy in waypoints])

    # Cumulative distance travelled: each interval contributes only the part of
    # itself that is not inside a dwell window.
    travelled = np.zeros(count, dtype=float)
    for i in range(1, count):
        moving = _moving_seconds(seconds[i - 1], seconds[i], dwells)
        travelled[i] = travelled[i - 1] + speed_mps * moving

    if travelled[-1] > path.length:
        raise ValueError(
            f"Path is {path.length:.0f} m but the trace needs {travelled[-1]:.0f} m; "
            "extend `waypoints`, shorten `duration_s`, or slow `speed_mps` down."
        )

    true_points = [path.interpolate(distance) for distance in travelled]
    true_xy = np.array([[p.x, p.y] for p in true_points])

    rng = np.random.default_rng(seed)
    noisy_xy = true_xy + rng.normal(0.0, noise_sigma_m, size=true_xy.shape)

    accuracy = np.full(
        count,
        noise_sigma_m if reported_accuracy_m is None else reported_accuracy_m,
        dtype=float,
    )

    # True instantaneous speed, as a well-behaved device would report it: zero
    # while dwelling. Nothing in the pipeline reads this, but a real trace has
    # it and its presence keeps the fixtures honest.
    reported_speed = np.zeros(count, dtype=float)
    for i in range(1, count):
        gap = seconds[i] - seconds[i - 1]
        reported_speed[i] = speed_mps if _moving_seconds(seconds[i - 1], seconds[i], dwells) > 0 else 0.0
    if count > 1:
        reported_speed[0] = reported_speed[1]

    for index in bad_accuracy_indices:
        _check_index(index, count, "bad_accuracy_indices")
        noisy_xy[index] += _random_offset(rng, bad_accuracy_offset_m)
        accuracy[index] = bad_accuracy_m

    for index in jump_indices:
        _check_index(index, count, "jump_indices")
        noisy_xy[index] += _random_offset(rng, jump_offset_m)

    stamps = pd.to_datetime(start_time, utc=True) + pd.to_timedelta(seconds, unit="s")

    def frame(xy: np.ndarray, accuracies: np.ndarray) -> gpd.GeoDataFrame:
        return gpd.GeoDataFrame(
            {
                "seq": np.arange(count, dtype=int),
                "recorded_at": stamps,
                "accuracy_m": accuracies,
                "speed_mps": reported_speed,
            },
            geometry=[Point(px, py) for px, py in xy],
            crs=UTM36S,
        )

    return SyntheticTrace(
        noisy=frame(noisy_xy, accuracy),
        truth=frame(true_xy, np.full(count, 0.0)),
        dwells=tuple(dwells),
        dropped_indices=tuple(sorted(set(bad_accuracy_indices) | set(jump_indices))),
    )


def _moving_seconds(start: float, end: float, dwells: tuple[tuple[float, float], ...]) -> float:
    """Seconds of `[start, end)` not covered by any dwell window."""
    stationary = 0.0
    for dwell_start, dwell_duration in dwells:
        dwell_end = dwell_start + dwell_duration
        stationary += max(0.0, min(end, dwell_end) - max(start, dwell_start))
    return max(0.0, (end - start) - stationary)


def _random_offset(rng: np.random.Generator, magnitude: float) -> np.ndarray:
    """A displacement of exactly `magnitude` metres in a random direction."""
    bearing = rng.uniform(0.0, 2.0 * np.pi)
    return np.array([magnitude * np.cos(bearing), magnitude * np.sin(bearing)])


def _check_index(index: int, count: int, label: str) -> None:
    if not 0 <= index < count:
        raise ValueError(f"{label} contains {index}, which is outside the trace (0..{count - 1}).")
