"""Dwell detection: find where a trace stood still long enough to be a stop.

A dwell event is a stretch of the trace over which the vehicle barely moved for
long enough that it was almost certainly picking up or dropping passengers.
Dwells are the raw material for stop clustering: many traces on the same route
dwell in roughly the same places, and those places are the kombi stops.

Input is expected to be a *cleaned* trace in EPSG:32736 — see `clean.py`. Raw
GPS noise on a stationary vehicle easily implies several m/s of apparent
movement, so running this on unsmoothed points finds nothing.
"""

from __future__ import annotations

from dataclasses import dataclass

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point

from clean import UTM36S, trace_seconds

#: A stretch must last at least this long to count as a dwell.
MIN_DWELL_S = 20.0

#: ...and the vehicle must stay under this speed throughout it.
MAX_DWELL_SPEED_MPS = 1.0


@dataclass(frozen=True)
class Dwell:
    """One detected dwell event.

    Attributes:
        centroid: Unweighted mean of the points in the dwell, in the trace's
            CRS (EPSG:32736).
        start_time: Timestamp of the first point of the dwell.
        duration_s: Seconds from the first to the last point of the dwell.
        start_index: Positional index of the first point in the trace.
        end_index: Positional index of the last point, inclusive.
    """

    centroid: Point
    start_time: pd.Timestamp
    duration_s: float
    start_index: int
    end_index: int

    @property
    def point_count(self) -> int:
        return self.end_index - self.start_index + 1

    @property
    def end_time(self) -> pd.Timestamp:
        return self.start_time + pd.Timedelta(seconds=self.duration_s)


def segment_speeds(gdf: gpd.GeoDataFrame) -> np.ndarray:
    """Return the speed in m/s over each consecutive pair of points.

    Length is `len(gdf) - 1`: element `i` covers the interval from point `i` to
    point `i + 1`. Segments that do not advance the clock get `inf`, so they
    never read as stationary — a zero-length interval is missing information,
    not evidence of a stop.
    """
    if len(gdf) < 2:
        return np.empty(0, dtype=float)

    xs = gdf.geometry.x.to_numpy(dtype=float)
    ys = gdf.geometry.y.to_numpy(dtype=float)
    seconds = trace_seconds(gdf)

    elapsed = np.diff(seconds)
    distance = np.hypot(np.diff(xs), np.diff(ys))
    return np.where(elapsed > 0, distance / np.where(elapsed > 0, elapsed, 1.0), np.inf)


def detect_dwells(
    gdf: gpd.GeoDataFrame,
    *,
    min_duration_s: float = MIN_DWELL_S,
    max_speed_mps: float = MAX_DWELL_SPEED_MPS,
) -> list[Dwell]:
    """Find every dwell event in a cleaned trace, in trace order.

    Speed is measured per *segment* rather than per point, which is what makes
    the reported duration exact: a run of slow segments `[a, b]` covers points
    `a` through `b + 1`, and the vehicle was under the threshold for the whole
    span from `t[a]` to `t[b + 1]`. Attributing a speed to each point instead
    would blur the endpoints by a sample either side.

    Runs are maximal — a single segment above the threshold splits a dwell in
    two, and neither half is reported unless it independently lasts
    `min_duration_s`.

    Args:
        gdf: Cleaned trace in EPSG:32736, ordered, with `recorded_at` set.
        min_duration_s: Shortest stretch that counts as a dwell.
        max_speed_mps: Speed the vehicle must stay under throughout.

    Returns:
        The dwells found, ordered by start time. Empty if the trace is shorter
        than two points.
    """
    if len(gdf) < 2:
        return []

    seconds = trace_seconds(gdf)
    xs = gdf.geometry.x.to_numpy(dtype=float)
    ys = gdf.geometry.y.to_numpy(dtype=float)
    stamps = pd.to_datetime(pd.Series(gdf["recorded_at"]).reset_index(drop=True), utc=True)

    slow = segment_speeds(gdf) < max_speed_mps

    # Maximal runs of slow segments: pad with False either side, then the +1
    # and -1 edges of the diff bracket each run.
    padded = np.concatenate(([False], slow, [False])).astype(np.int8)
    edges = np.diff(padded)
    run_starts = np.flatnonzero(edges == 1)
    run_ends = np.flatnonzero(edges == -1) - 1  # inclusive segment index

    dwells: list[Dwell] = []
    for first_segment, last_segment in zip(run_starts, run_ends, strict=True):
        first = int(first_segment)
        last = int(last_segment) + 1  # a run of segments spans one extra point
        duration = float(seconds[last] - seconds[first])
        if duration < min_duration_s:
            continue

        dwells.append(
            Dwell(
                centroid=Point(
                    float(np.mean(xs[first : last + 1])),
                    float(np.mean(ys[first : last + 1])),
                ),
                start_time=stamps.iloc[first],
                duration_s=duration,
                start_index=first,
                end_index=last,
            )
        )

    return dwells


def dwells_to_gdf(dwells: list[Dwell], crs: str = UTM36S) -> gpd.GeoDataFrame:
    """Collect dwells into a GeoDataFrame for export or clustering.

    Kept separate from `detect_dwells` so that detection stays a plain,
    easily-asserted list of values; downstream stages that want to cluster
    dwells across many traces call this.
    """
    return gpd.GeoDataFrame(
        {
            "start_time": [d.start_time for d in dwells],
            "duration_s": [d.duration_s for d in dwells],
            "point_count": [d.point_count for d in dwells],
            "start_index": [d.start_index for d in dwells],
            "end_index": [d.end_index for d in dwells],
        },
        geometry=[d.centroid for d in dwells],
        crs=crs,
    )
