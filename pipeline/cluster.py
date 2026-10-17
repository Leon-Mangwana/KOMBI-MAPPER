"""Clustering: many matched traces in, canonical routes and stops out.

This is the stage that turns a pile of individual rides into the network. Twenty
contributors riding the Mbare–Msasa kombi produce twenty matched traces that all
follow the same roads; clustering recognises them as one route, picks one of
them to stand for it, and works out where along it the kombi actually stops.

Two clusterings run, over different things:

**Routes.** Each matched trace is simplified with Douglas–Peucker (and then
resampled — see `RESAMPLE_STEP_M`, which is not optional) and compared to every
other with the discrete Fréchet distance (`frechet.py`), giving a full pairwise
distance matrix. DBSCAN over that matrix with `metric="precomputed"` groups the
traces; each cluster is a route, and the route's `canonical_geom` is its
**medoid** — the member trace with the smallest total distance to the others.

DBSCAN, and not k-means: the number of kombi routes in Harare is exactly what we
are trying to find out, most of what gets uploaded is a one-off that belongs to
no cluster at all, and "route" is not a shape with a mean — the average of two
traces down parallel streets runs through the buildings between them. DBSCAN
needs no `k`, labels the one-offs `-1` instead of forcing them somewhere, and
with a medoid the canonical geometry is always a real trace that a real kombi
really drove.

**Stops.** Separately, every dwell that `stops.detect_dwells` found on any trace
is thrown into one pile and clustered on plain Euclidean distance — dwells are
points, and points do have a mean. A cluster corroborated by enough distinct
traces is a stop, sited at the mean of its dwells.

The two are then joined: a stop belongs to a route when a trace of that route
dwelled there, and the stops of a route are ordered by projecting them onto its
`canonical_geom` with Shapely's `line_locate_point`. That projection is what
makes the ordering right where a straight-line or nearest-neighbour ordering
would not be — a route that doubles back past a rank visits it twice at
different distances along the line, and consecutive stops on a winding road are
ordered by road distance rather than by how close together they happen to be.

Everything metric happens in **EPSG:32736**; geometry is reprojected to
EPSG:4326 only on the way into the database. See ../CLAUDE.md.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, replace

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from shapely.geometry import LineString, Point
from sklearn.cluster import DBSCAN
from sqlalchemy import Engine, bindparam, text

from clean import UTM36S, WGS84, clean_trace, load_trace
from config import get_engine
from frechet import discrete_frechet
from stops import Dwell, detect_dwells

logger = logging.getLogger(__name__)

#: Douglas–Peucker tolerance for the traces fed to the Fréchet comparison.
#:
#: Cost, mostly. Fréchet is O(nm) per pair and the number of pairs is quadratic
#: in the number of traces, so dropping a 40-minute trace from ~2400 vertices to
#: a couple of hundred is the difference between a clustering run of minutes and
#: one of hours. It also removes the sub-lane wiggle that OSRM's road geometry
#: carries, which is detail no comparison between two *routes* should be
#: sensitive to.
#:
#: 15 m is comfortably below the width of the roads being distinguished, so no
#: real difference in route is simplified away.
SIMPLIFY_TOLERANCE_M = 15.0

#: Vertex spacing the simplified lines are resampled to before comparison.
#:
#: Douglas–Peucker on its own is not enough, and the reason is worth spelling
#: out because it is not obvious and it silently breaks the clustering.
#:
#: The *discrete* Fréchet distance can only ever pair a vertex with a vertex
#: (see `frechet.discrete_frechet`). Douglas–Peucker keeps a vertex only where
#: the line bends, so a 3 km straight leg comes back with vertices at its two
#: ends and nothing in between. Two traces down that same leg are simplified
#: independently, and a bend that clears the tolerance on one may not on the
#: other — so one line ends up with a vertex mid-leg that has no partner within
#: kilometres on the other. The pairing distance is then the distance to the far
#: end of the leg, and two traces lying within 10 m of each other for their whole
#: length measure hundreds of metres apart. This is not hypothetical: it is what
#: the first run of `tests/test_cluster.py` did, at 365 m on a corridor whose
#: traces never diverge by more than about 10 m.
#:
#: No choice of `eps` fixes that, because the error scales with the length of
#: the longest straight leg rather than with anything about the routes. Putting
#: vertices back at a fixed arc-length spacing does fix it: every vertex on one
#: line then has a partner within half a step on the other, which bounds the
#: sampling artefact at `step / 2` instead of leaving it unbounded.
#:
#: Simplify *then* resample, in that order: Douglas–Peucker decides the shape and
#: throws away the noise, and resampling only redistributes vertices along the
#: shape it settled on. 50 m caps the artefact at 25 m — well inside `ROUTE_EPS_M`
#: — and still leaves a 12 km route at about 240 vertices.
RESAMPLE_STEP_M = 50.0

#: DBSCAN radius for route clustering: the Fréchet distance under which two
#: matched traces are taken to be the same route.
#:
#: Fréchet is a *maximum* over the coupling, so this is not "how far apart the
#: traces are on average" — it is the single worst pairing, and on two traces of
#: the same route that is nearly always at the ends. Contributors board and
#: alight where they live, not at the terminus, so two honest recordings of the
#: same route routinely differ by a few hundred metres of tail while agreeing to
#: within a lane for the 12 km in between. The middle costs nothing; the ends
#: set the number.
#:
#: 200 m is roughly two Harare city blocks. Below ~120 m the clusters fragment by
#: boarding point; much above ~300 m, routes that share a long trunk corridor and
#: split at the end start merging. To retune on real data, print the sorted
#: distance to each trace's k-th nearest neighbour and look for the knee — the
#: standard DBSCAN eps heuristic, and the distance matrix is already to hand.
ROUTE_EPS_M = 200.0

#: Traces needed before a cluster is called a route. Two independent recordings
#: is the minimum that means anything: one is unfalsifiable, and a route nobody
#: else has ever ridden should stay `-1` until somebody does.
ROUTE_MIN_TRACES = 2

#: DBSCAN radius for stop clustering, in metres. A kombi does not stop at a
#: marked bay; it stops wherever the conductor and the passenger agree, which
#: smears the dwells of one stop over some tens of metres of kerb. 40 m spans
#: that without reaching across an intersection to the stop on the far side.
STOP_EPS_M = 40.0

#: Distinct traces that must dwell in a cluster before it is called a stop.
#:
#: Distinct *traces*, deliberately, not dwell events. Three dwells from the same
#: ride is one kombi stuck at one robot in stop-start traffic, which the dwell
#: bridging in `stops.py` does not always merge; it is one observation, not
#: three, and corroboration has to come from independent rides.
STOP_MIN_SUPPORT = 3

#: How far a stop may sit from a route's canonical geometry and still be
#: attributed to it. A stop is attributed to a route because one of the route's
#: traces dwelled there — but a contributor who left the recording running while
#: walking home produces dwells nowhere near the route, and projecting one of
#: those onto the line with `line_locate_point` would file it at whatever
#: distance happened to be nearest. 60 m clears any plausible kerb-to-centreline
#: offset while rejecting a dwell that is simply somewhere else.
MAX_STOP_OFFSET_M = 60.0


@dataclass(frozen=True)
class RouteCluster:
    """One cluster of traces: a route.

    Attributes:
        label: The DBSCAN label. Cluster identity within a run only — it is not
            an id and does not survive to the database.
        trace_ids: Every trace in the cluster, ascending.
        medoid_trace_id: The trace chosen to represent the route.
        canonical_geom: That trace's matched geometry at full resolution, in
            EPSG:32736.
        stop_labels: Labels of this route's stops, in order along
            `canonical_geom`. Empty until `order_route_stops` has run.
    """

    label: int
    trace_ids: tuple[int, ...]
    medoid_trace_id: int
    canonical_geom: LineString
    stop_labels: tuple[int, ...] = ()

    @property
    def support(self) -> int:
        """Number of traces backing this route."""
        return len(self.trace_ids)

    @property
    def length_m(self) -> float:
        return float(self.canonical_geom.length)


@dataclass(frozen=True)
class StopCluster:
    """One cluster of dwell centroids: a stop.

    Attributes:
        label: The DBSCAN label, unique within a run. See `RouteCluster.label`.
        centroid: Mean of the member dwell centroids, in EPSG:32736.
        trace_ids: The distinct traces that dwelled here, ascending.
        dwell_count: Number of dwell events in the cluster, which is at least
            `len(trace_ids)` and larger when a trace dwelled here twice — an
            out-and-back route passing the same stop in both directions.
        median_dwell_s: Median dwell duration. A useful sanity check when
            reviewing output: a "stop" with a 4-minute median is a rank or a
            traffic queue, not a pickup point.
    """

    label: int
    centroid: Point
    trace_ids: tuple[int, ...]
    dwell_count: int
    median_dwell_s: float

    @property
    def support(self) -> int:
        """Distinct traces that dwelled here. See `STOP_MIN_SUPPORT`."""
        return len(self.trace_ids)


@dataclass(frozen=True)
class Network:
    """The full result of a clustering run, before it is persisted.

    Stops are held once here and referenced by label from
    `RouteCluster.stop_labels`, because one stop genuinely serves several routes
    — that is what a shared corridor is — and duplicating it per route would put
    two ids on one kerb.
    """

    routes: tuple[RouteCluster, ...]
    stops: tuple[StopCluster, ...]

    @property
    def stops_by_label(self) -> dict[int, StopCluster]:
        return {stop.label: stop for stop in self.stops}

    def summary(self) -> str:
        served = sum(len(route.stop_labels) for route in self.routes)
        return (
            f"{len(self.routes)} route(s) over "
            f"{sum(route.support for route in self.routes)} trace(s); "
            f"{len(self.stops)} stop(s), {served} route-stop link(s)"
        )


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


_MATCHED_TRACES_SQL = text(
    """
    SELECT id AS trace_id, ST_AsText(matched_geom) AS wkt
    FROM traces
    WHERE matched_geom IS NOT NULL
      AND ST_NumPoints(matched_geom) >= 2
    ORDER BY id
    """
)


def load_matched_traces(engine: Engine | None = None) -> gpd.GeoDataFrame:
    """Load every map-matched trace, reprojected to EPSG:32736.

    Returns a GeoDataFrame with `trace_id` and a LineString `geometry`, ordered
    by trace id. Traces that have not been through `match.py` have no
    `matched_geom` and are skipped — clustering raw or merely-cleaned traces
    would compare GPS noise rather than roads.

    The result may be empty; every function downstream tolerates that.
    """
    engine = engine if engine is not None else get_engine()
    with engine.connect() as conn:
        frame = pd.read_sql(_MATCHED_TRACES_SQL, conn)

    if frame.empty:
        logger.warning("No matched traces found; run match.py before clustering.")
        return gpd.GeoDataFrame(
            {"trace_id": pd.Series(dtype=int)},
            geometry=gpd.GeoSeries([], crs=WGS84),
            crs=WGS84,
        ).to_crs(UTM36S)

    gdf = gpd.GeoDataFrame(
        {"trace_id": frame["trace_id"].astype(int)},
        geometry=shapely.from_wkt(frame["wkt"].to_numpy()),
        crs=WGS84,
    )
    return gdf.to_crs(UTM36S).reset_index(drop=True)


def collect_dwells(
    trace_ids: list[int] | np.ndarray,
    engine: Engine | None = None,
    **dwell_kwargs: float,
) -> gpd.GeoDataFrame:
    """Clean each trace, detect its dwells, and pool them into one frame.

    Dwells are not persisted anywhere — they are cheap to recompute and their
    detection thresholds are still being tuned, so caching them would mean
    invalidating a table every time `stops.MIN_DWELL_S` moved. They are
    recomputed here from the stored points.

    A trace that fails to load or is too short to have dwells is logged and
    skipped rather than failing the run: one bad upload should not cost the
    whole network.

    Args:
        trace_ids: Traces to look at. Normally the ones that clustered into a
            route — a trace nobody can corroborate is a poor source of stops.
        engine: Database engine. Defaults to the process-wide one.
        **dwell_kwargs: Forwarded to `stops.detect_dwells`.

    Returns:
        A GeoDataFrame in EPSG:32736 with `trace_id`, `duration_s`,
        `start_time` and the dwell centroid as `geometry`.
    """
    rows: list[tuple[int, Dwell]] = []
    for trace_id in (int(value) for value in trace_ids):
        try:
            cleaned = clean_trace(load_trace(trace_id, engine=engine))
        except (LookupError, ValueError) as error:
            # `LookupError`: no points. `ValueError`: `clean_trace` could not
            # derive speeds — no timestamps, or too few fixes left after the
            # accuracy gates. `process.py` rejects such traces, but one can
            # still carry a `matched_geom` (seeded with `--fake-match`, say),
            # and one bad upload must not cost the whole network its stops.
            logger.warning("Trace %d skipped for stop detection: %s", trace_id, error)
            continue

        found = detect_dwells(cleaned, **dwell_kwargs)  # type: ignore[arg-type]
        logger.debug("Trace %d contributed %d dwell(s).", trace_id, len(found))
        rows.extend((trace_id, dwell) for dwell in found)

    return gpd.GeoDataFrame(
        {
            "trace_id": pd.Series([trace_id for trace_id, _ in rows], dtype=int),
            "duration_s": pd.Series([dwell.duration_s for _, dwell in rows], dtype=float),
            "start_time": [dwell.start_time for _, dwell in rows],
        },
        geometry=[dwell.centroid for _, dwell in rows],
        crs=UTM36S,
    )


# --------------------------------------------------------------------------
# Route clustering
# --------------------------------------------------------------------------


def simplify_traces(
    geometries: list[LineString], tolerance_m: float = SIMPLIFY_TOLERANCE_M
) -> list[LineString]:
    """Douglas–Peucker each line down to `tolerance_m`. See `SIMPLIFY_TOLERANCE_M`.

    `preserve_topology=False` selects Shapely's plain Douglas–Peucker. The
    topology-preserving variant exists to stop a *polygon* self-intersecting as
    it collapses; these are open lines being measured against each other, and
    the guarantee costs speed for nothing here.

    Douglas–Peucker always keeps the first and last vertex, so a simplified
    trace still has at least two points and still starts and ends where it did —
    which matters, because those endpoints are what the Fréchet distance is most
    sensitive to.

    This is half of the preparation a trace needs; `resample_traces` is the
    other half, and running this one alone is a trap. See `RESAMPLE_STEP_M`.
    """
    return [geometry.simplify(tolerance_m, preserve_topology=False) for geometry in geometries]


def resample_traces(
    geometries: list[LineString], step_m: float = RESAMPLE_STEP_M
) -> list[LineString]:
    """Put vertices back at a uniform spacing along each line.

    The step is the *largest* gap that comes out: each line is cut into the
    fewest equal pieces no longer than `step_m`, so the spacing is a little
    under it and the endpoints are landed on exactly rather than being
    overshot. The shape is untouched — every new vertex lies on the line it came
    from — and only the sampling changes.

    Why this exists at all, and why it runs after Douglas–Peucker rather than
    instead of it, is `RESAMPLE_STEP_M`.
    """
    if step_m <= 0:
        raise ValueError(f"step_m must be positive; got {step_m}.")

    resampled: list[LineString] = []
    for geometry in geometries:
        length = geometry.length
        if length == 0.0:
            # A trace that never went anywhere. Nothing to redistribute, and
            # `linspace` over a zero range would only make duplicate vertices.
            resampled.append(geometry)
            continue

        steps = int(np.ceil(length / step_m))
        distances = np.linspace(0.0, length, steps + 1)
        points = shapely.line_interpolate_point(geometry, distances)
        resampled.append(LineString(shapely.get_coordinates(points)))

    return resampled


def pairwise_frechet(
    geometries: list[LineString], *, max_distance: float | None = None
) -> np.ndarray:
    """Symmetric matrix of discrete Fréchet distances in metres, zero diagonal.

    Args:
        geometries: Lines in EPSG:32736. Put them through `simplify_traces` and
            then `resample_traces` first — a raw pair of matched traces will
            give a badly inflated distance. See `RESAMPLE_STEP_M`.
        max_distance: Passed to `frechet.discrete_frechet` as an early exit.
            Entries above it become lower bounds rather than exact distances,
            which is all a DBSCAN at `eps <= max_distance` needs but is wrong
            for anything that reads the numbers themselves. Left unset the
            matrix is exact.

    Returns:
        An `(n, n)` array of `float`.
    """
    coordinates = [shapely.get_coordinates(geometry) for geometry in geometries]
    count = len(coordinates)

    distances = np.zeros((count, count), dtype=float)
    for i in range(count):
        for j in range(i + 1, count):
            distance = discrete_frechet(coordinates[i], coordinates[j], max_distance=max_distance)
            distances[i, j] = distances[j, i] = distance

    return distances


def cluster_routes(
    traces: gpd.GeoDataFrame,
    *,
    tolerance_m: float = SIMPLIFY_TOLERANCE_M,
    step_m: float = RESAMPLE_STEP_M,
    eps_m: float = ROUTE_EPS_M,
    min_traces: int = ROUTE_MIN_TRACES,
    prune: bool = True,
) -> list[RouteCluster]:
    """Group matched traces into routes and pick each route's canonical geometry.

    Args:
        traces: Output of `load_matched_traces` — `trace_id` plus a LineString
            `geometry` in EPSG:32736.
        tolerance_m: Douglas–Peucker tolerance. See `SIMPLIFY_TOLERANCE_M`.
        step_m: Vertex spacing to resample to afterwards. See `RESAMPLE_STEP_M`.
        eps_m: DBSCAN radius in metres. See `ROUTE_EPS_M`.
        min_traces: DBSCAN `min_samples`, counting the trace itself, so 2 means
            "at least one other trace agrees". See `ROUTE_MIN_TRACES`.
        prune: Let the Fréchet computation give up on a pair once it is known to
            exceed `eps_m`. Faster by a large factor on a real batch, where most
            pairs are nowhere near each other, and it cannot change the
            clustering — DBSCAN only ever asks whether a distance is within
            `eps`. Turn it off to compare against an exact matrix.

    Returns:
        One `RouteCluster` per cluster, ordered by descending support so the
        best-attested routes come first. Traces DBSCAN labelled `-1` appear in
        no cluster.
    """
    if len(traces) == 0:
        return []

    trace_ids = traces["trace_id"].to_numpy(dtype=int)
    geometries = list(traces.geometry)
    prepared = resample_traces(simplify_traces(geometries, tolerance_m), step_m)

    logger.info(
        "Comparing %d trace(s): %d vertices simplified at %.0f m and resampled to %d at %.0f m.",
        len(prepared),
        sum(shapely.get_num_coordinates(g) for g in geometries),
        tolerance_m,
        sum(shapely.get_num_coordinates(g) for g in prepared),
        step_m,
    )
    distances = pairwise_frechet(prepared, max_distance=eps_m if prune else None)

    labels = DBSCAN(eps=eps_m, min_samples=min_traces, metric="precomputed").fit_predict(distances)

    clusters: list[RouteCluster] = []
    for label in sorted({int(value) for value in labels} - {-1}):
        members = np.flatnonzero(labels == label)

        # The medoid is chosen from *exact* distances. A DBSCAN cluster is
        # density-connected, not a ball: two members can be chained together
        # through intermediates and be far further apart than `eps_m`, so the
        # pruned entries above are precisely the ones that would skew a sum of
        # distances. Recomputing within the cluster is cheap — clusters are
        # small, and the pairs inside one are mostly close enough that the
        # pruning never fired on them anyway.
        if prune and len(members) > 2:
            local = pairwise_frechet([prepared[index] for index in members])
        else:
            local = distances[np.ix_(members, members)]

        medoid = members[int(np.argmin(local.sum(axis=1)))]
        clusters.append(
            RouteCluster(
                label=label,
                trace_ids=tuple(int(trace_ids[index]) for index in members),
                medoid_trace_id=int(trace_ids[medoid]),
                # Full resolution, not the simplified line: simplification is a
                # comparison device, and shipping its 15 m corner-cutting to the
                # map would visibly clip every turn.
                canonical_geom=geometries[medoid],
            )
        )

    unclustered = int(np.sum(labels == -1))
    if unclustered:
        logger.info(
            "%d of %d trace(s) matched no route; they need a corroborating ride.",
            unclustered,
            len(labels),
        )

    return sorted(clusters, key=lambda route: (-route.support, route.medoid_trace_id))


# --------------------------------------------------------------------------
# Stop clustering
# --------------------------------------------------------------------------


def cluster_stops(
    dwells: gpd.GeoDataFrame,
    *,
    eps_m: float = STOP_EPS_M,
    min_support: int = STOP_MIN_SUPPORT,
    min_samples: int | None = None,
) -> list[StopCluster]:
    """Cluster pooled dwell centroids into stops.

    Plain Euclidean DBSCAN in metres — these are points rather than paths, so
    none of the Fréchet machinery applies and `metric="euclidean"` on the
    projected coordinates is both correct and fast.

    Args:
        dwells: Output of `collect_dwells`: dwell centroids in EPSG:32736 with a
            `trace_id` and a `duration_s`.
        eps_m: DBSCAN radius in metres. See `STOP_EPS_M`.
        min_support: Distinct traces a cluster needs to survive. See
            `STOP_MIN_SUPPORT`.
        min_samples: DBSCAN `min_samples`, i.e. dwell *events* needed to seed a
            cluster. Defaults to `min_support`, which is the natural pairing;
            the two differ because DBSCAN cannot count distinct traces, so the
            trace-level threshold is applied afterwards.

    Returns:
        One `StopCluster` per surviving cluster, ordered by descending support.
    """
    if len(dwells) == 0:
        return []

    samples = min_support if min_samples is None else min_samples
    xy = np.column_stack(
        [
            dwells.geometry.x.to_numpy(dtype=float),
            dwells.geometry.y.to_numpy(dtype=float),
        ]
    )
    trace_ids = dwells["trace_id"].to_numpy(dtype=int)
    durations = dwells["duration_s"].to_numpy(dtype=float)

    labels = DBSCAN(eps=eps_m, min_samples=samples, metric="euclidean").fit_predict(xy)

    stops: list[StopCluster] = []
    rejected = 0
    for label in sorted({int(value) for value in labels} - {-1}):
        members = np.flatnonzero(labels == label)
        contributors = tuple(sorted({int(trace_ids[index]) for index in members}))

        if len(contributors) < min_support:
            # Repeated dwells from one ride: one observation, not several.
            rejected += 1
            continue

        centre = xy[members].mean(axis=0)
        stops.append(
            StopCluster(
                label=label,
                centroid=Point(float(centre[0]), float(centre[1])),
                trace_ids=contributors,
                dwell_count=len(members),
                median_dwell_s=float(np.median(durations[members])),
            )
        )

    if rejected:
        logger.info(
            "Dropped %d dwell cluster(s) backed by fewer than %d distinct trace(s).",
            rejected,
            min_support,
        )

    return sorted(stops, key=lambda stop: (-stop.support, stop.label))


def order_route_stops(
    routes: list[RouteCluster],
    stops: list[StopCluster],
    *,
    max_offset_m: float = MAX_STOP_OFFSET_M,
) -> list[RouteCluster]:
    """Attach each route's stops to it, ordered along its canonical geometry.

    A stop belongs to a route when at least one of the route's traces dwelled
    there — that is what the stop was built out of, so no extra spatial test is
    needed to establish the association, only `max_offset_m` to reject a dwell
    the contributor recorded off-route entirely.

    Ordering is by `shapely.line_locate_point`: the distance along
    `canonical_geom` of the point on it nearest the stop. That is the stop's
    position *as the kombi drives it*, which is what an ordered stop list means
    and what GTFS `stop_sequence` will need.

    Returns:
        New `RouteCluster` values with `stop_labels` set; the input is unchanged.
    """
    ordered: list[RouteCluster] = []
    for route in routes:
        members = set(route.trace_ids)
        positions: list[tuple[float, int]] = []

        for stop in stops:
            if members.isdisjoint(stop.trace_ids):
                continue

            offset = route.canonical_geom.distance(stop.centroid)
            if offset > max_offset_m:
                logger.debug(
                    "Stop %d sits %.0f m off route %d; not attributing it.",
                    stop.label,
                    offset,
                    route.label,
                )
                continue

            along = float(shapely.line_locate_point(route.canonical_geom, stop.centroid))
            positions.append((along, stop.label))

        # Ties break on label so a rerun on the same input gives the same order.
        positions.sort()
        ordered.append(replace(route, stop_labels=tuple(label for _, label in positions)))

    return ordered


# --------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------


def build_network(
    traces: gpd.GeoDataFrame,
    dwells: gpd.GeoDataFrame,
    *,
    tolerance_m: float = SIMPLIFY_TOLERANCE_M,
    step_m: float = RESAMPLE_STEP_M,
    route_eps_m: float = ROUTE_EPS_M,
    min_traces: int = ROUTE_MIN_TRACES,
    stop_eps_m: float = STOP_EPS_M,
    min_support: int = STOP_MIN_SUPPORT,
    max_offset_m: float = MAX_STOP_OFFSET_M,
) -> Network:
    """Run both clusterings over already-loaded data and join them.

    The pure part of the stage: no database, no I/O. `run` is the version that
    reads and writes; this is what the tests drive.
    """
    routes = cluster_routes(
        traces,
        tolerance_m=tolerance_m,
        step_m=step_m,
        eps_m=route_eps_m,
        min_traces=min_traces,
    )
    stops = cluster_stops(dwells, eps_m=stop_eps_m, min_support=min_support)
    return _join(routes, stops, max_offset_m=max_offset_m)


def _join(routes: list[RouteCluster], stops: list[StopCluster], *, max_offset_m: float) -> Network:
    """Order each route's stops along it and drop the stops nothing serves."""
    routes = order_route_stops(routes, stops, max_offset_m=max_offset_m)

    # A stop nothing serves is real enough as a place people got off, but with
    # no route to hang it on it cannot be exported to GTFS, and a stops table
    # full of orphans is hard to review. Dropped here rather than in
    # `cluster_stops`, which has no idea what the routes are.
    attached = {label for route in routes for label in route.stop_labels}
    kept = tuple(stop for stop in stops if stop.label in attached)
    if len(kept) < len(stops):
        logger.info("Dropped %d stop(s) that no route passes.", len(stops) - len(kept))

    return Network(routes=tuple(routes), stops=kept)


def run(
    engine: Engine | None = None,
    *,
    replace_existing: bool = False,
    carry_names: bool = True,
    persist: bool = True,
    tolerance_m: float = SIMPLIFY_TOLERANCE_M,
    step_m: float = RESAMPLE_STEP_M,
    route_eps_m: float = ROUTE_EPS_M,
    min_traces: int = ROUTE_MIN_TRACES,
    stop_eps_m: float = STOP_EPS_M,
    min_support: int = STOP_MIN_SUPPORT,
    max_offset_m: float = MAX_STOP_OFFSET_M,
) -> Network:
    """Load, cluster, and (by default) persist. The end-to-end entry point.

    Routes are clustered first and dwells are then collected only from the
    traces that made it into one. A trace nobody corroborates is as likely to be
    a car ride or a walk as a kombi, and its dwells — outside a front gate, at a
    supermarket — would be noise in the stop clustering. It also keeps the
    expensive half of the stage off traces that are about to be ignored.

    Args:
        engine: Database engine. Defaults to the process-wide one.
        replace_existing: Clear the derived tables first. See `persist_network`.
        carry_names: Re-apply existing route and stop names to the new
            clustering. See `naming.carry_over`.
        persist: Set `False` for a dry run — cluster and report, write nothing.
        tolerance_m, step_m, route_eps_m, min_traces, stop_eps_m, min_support,
        max_offset_m: As `build_network`.

    Returns:
        The `Network`, whether or not it was persisted.
    """
    engine = engine if engine is not None else get_engine()

    traces = load_matched_traces(engine)
    routes = cluster_routes(
        traces,
        tolerance_m=tolerance_m,
        step_m=step_m,
        eps_m=route_eps_m,
        min_traces=min_traces,
    )

    clustered_ids = sorted({trace_id for route in routes for trace_id in route.trace_ids})
    dwells = collect_dwells(clustered_ids, engine=engine)
    logger.info(
        "Collected %d dwell(s) from %d clustered trace(s).", len(dwells), len(clustered_ids)
    )

    stops = cluster_stops(dwells, eps_m=stop_eps_m, min_support=min_support)
    network = _join(routes, stops, max_offset_m=max_offset_m)
    logger.info("Clustered %s.", network.summary())

    if persist:
        persist_network(
            network,
            engine=engine,
            replace_existing=replace_existing,
            carry_names=carry_names,
        )
    return network


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Persisted:
    """Database ids assigned to a persisted `Network`, keyed by cluster label."""

    route_ids: dict[int, int]
    stop_ids: dict[int, int]


_INSERT_ROUTE_SQL = text(
    """
    INSERT INTO routes (short_name, long_name, geom)
    VALUES (:short_name, :long_name, ST_GeomFromText(:wkt, 4326))
    RETURNING id
    """
)

_INSERT_STOP_SQL = text(
    """
    INSERT INTO stops (name, geom)
    VALUES (:name, ST_GeomFromText(:wkt, 4326))
    RETURNING id
    """
)

_INSERT_ROUTE_STOP_SQL = text(
    """
    INSERT INTO route_stops (route_id, stop_id, seq)
    VALUES (:route_id, :stop_id, :seq)
    """
)

_ASSIGN_TRACES_SQL = text(
    """
    UPDATE traces
    SET route_id = :route_id
    WHERE id IN :trace_ids
    """
).bindparams(bindparam("trace_ids", expanding=True))


def persist_network(
    network: Network,
    engine: Engine | None = None,
    *,
    replace_existing: bool = False,
    carry_names: bool = True,
) -> Persisted:
    """Write routes, stops and route_stops, and point each trace at its route.

    One transaction: a half-written network — routes with no stops, or
    `route_stops` referring to rows that were rolled back — is worse than none.

    Geometry goes in as EPSG:4326, reprojected from the EPSG:32736 that
    everything upstream works in.

    **Re-running.** `routes`, `stops` and `route_stops` are derived tables, so a
    second run over more traces should normally *replace* what the first one
    wrote rather than adding a parallel set of routes beside it. Pass
    `replace_existing=True`; the deletes cascade to `route_stops` and null out
    `traces.route_id`.

    **Names survive that rebuild.** `routes.short_name`, `routes.long_name` and
    `stops.name` are the columns this stage cannot fill in and a person has to,
    so deleting them would throw away the only data in the system nobody can
    recompute. Before the delete, each new cluster is matched to the named row
    it evidently replaces and inherits its name — see `naming.carry_over`. Pass
    `carry_names=False` to rebuild the names away deliberately.

    Args:
        network: The clustering to write.
        engine: Database engine. Defaults to the process-wide one.
        replace_existing: Clear the derived tables first.
        carry_names: Re-apply existing names to the matching new clusters. Only
            meaningful with `replace_existing`, since nothing is deleted
            otherwise.

    Returns:
        The ids assigned, keyed by cluster label.
    """
    engine = engine if engine is not None else get_engine()

    route_names: dict[int, tuple[str | None, str | None]] = {}
    stop_names: dict[int, str] = {}

    with engine.begin() as conn:
        if replace_existing:
            if carry_names:
                # Inside the transaction and before the delete: these rows are
                # about to stop existing, and a name not read now is lost.
                #
                # Imported here rather than at module scope because `naming`
                # imports from this module, and at module scope that is a cycle.
                from naming import carry_over

                carried = carry_over(network, conn)
                route_names, stop_names = carried.routes, carried.stops

            # `route_stops` cascades from both parents and `traces.route_id` is
            # ON DELETE SET NULL, so these two deletes are the whole rebuild.
            deleted_routes = conn.execute(text("DELETE FROM routes")).rowcount
            deleted_stops = conn.execute(text("DELETE FROM stops")).rowcount
            logger.warning(
                "Replaced the previous clustering: deleted %d route(s) and %d stop(s).",
                deleted_routes,
                deleted_stops,
            )

        stop_ids: dict[int, int] = {}
        for stop in network.stops:
            stop_ids[stop.label] = int(
                conn.execute(
                    _INSERT_STOP_SQL,
                    {
                        "wkt": _to_wgs84(stop.centroid).wkt,
                        "name": stop_names.get(stop.label),
                    },
                ).scalar_one()
            )

        route_ids: dict[int, int] = {}
        for route in network.routes:
            short_name, long_name = route_names.get(route.label, (None, None))
            route_id = int(
                conn.execute(
                    _INSERT_ROUTE_SQL,
                    {
                        "wkt": _to_wgs84(route.canonical_geom).wkt,
                        "short_name": short_name,
                        "long_name": long_name,
                    },
                ).scalar_one()
            )
            route_ids[route.label] = route_id

            links = [
                {"route_id": route_id, "stop_id": stop_ids[label], "seq": seq}
                for seq, label in enumerate(route.stop_labels)
            ]
            if links:
                conn.execute(_INSERT_ROUTE_STOP_SQL, links)

            conn.execute(
                _ASSIGN_TRACES_SQL,
                {"route_id": route_id, "trace_ids": list(route.trace_ids)},
            )

    logger.info("Persisted %d route(s) and %d stop(s).", len(network.routes), len(network.stops))
    return Persisted(route_ids=route_ids, stop_ids=stop_ids)


def _to_wgs84(geometry: LineString | Point) -> LineString | Point:
    """Reproject one EPSG:32736 geometry to EPSG:4326, as stored."""
    return gpd.GeoSeries([geometry], crs=UTM36S).to_crs(WGS84).iloc[0]


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--replace",
        action="store_true",
        help="delete the existing routes and stops first (see persist_network)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="cluster and report, but write nothing",
    )
    parser.add_argument(
        "--forget-names",
        action="store_true",
        help="do not carry existing route and stop names onto the new clustering",
    )
    parser.add_argument(
        "--route-eps-m",
        type=float,
        default=ROUTE_EPS_M,
        help=f"Frechet radius for route clustering (default {ROUTE_EPS_M:.0f})",
    )
    parser.add_argument(
        "--stop-eps-m",
        type=float,
        default=STOP_EPS_M,
        help=f"radius for dwell clustering (default {STOP_EPS_M:.0f})",
    )
    parser.add_argument(
        "--min-support",
        type=int,
        default=STOP_MIN_SUPPORT,
        help=f"distinct traces needed for a stop (default {STOP_MIN_SUPPORT})",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    network = run(
        replace_existing=args.replace,
        carry_names=not args.forget_names,
        persist=not args.dry_run,
        route_eps_m=args.route_eps_m,
        stop_eps_m=args.stop_eps_m,
        min_support=args.min_support,
    )

    print(network.summary())
    for route in network.routes:
        print(
            f"  route {route.label}: {route.support} trace(s), "
            f"{route.length_m / 1000:.1f} km, {len(route.stop_labels)} stop(s), "
            f"canonical = trace {route.medoid_trace_id}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
