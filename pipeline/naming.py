"""Carrying route and stop names across a re-clustering run.

Clustering cannot name anything. A cluster of GPS traces has no idea it is the
Mbare route, so `routes.short_name`, `routes.long_name` and `stops.name` are
filled in by a person who knows Harare — and that is the most expensive data in
the system, because it is the only part nobody can recompute.

Re-clustering used to destroy it. `cluster.persist_network(replace_existing=True)`
deletes the derived tables and rebuilds them, which is the right thing to do
with geometry — more traces mean better routes — and the wrong thing to do with
names. The alternative, running without `--replace`, adds a second set of routes
beside the first, so the operator's choice was duplicates or amnesia. Neither is
a choice anybody should have to make twice.

This module is the third option: before the rebuild, remember what was named;
after it, give each new cluster the name of the old one it evidently replaces.

**How a new route is recognised as an old one.** By the same measure that
decided two traces were the same route in the first place — discrete Fréchet
distance, on geometries resampled the same way `cluster.cluster_routes`
resamples them. If a corridor is still the same corridor, its canonical line
moves by metres, far inside `cluster.ROUTE_EPS_M`. If a route has genuinely
changed shape, it exceeds the threshold and arrives unnamed, which is the
correct outcome: somebody should look at it.

Fréchet is direction-sensitive and that is deliberate here. An inbound and an
outbound route along one road are separate clusters with separate names, and
matching against a reversed geometry would hand each the other's name.

**Matching is one-to-one.** Two new routes that both resemble one old named
route cannot both be it — the closer one takes the name and the other is left
for review. A greedy pass over the candidate pairs in ascending distance does
that, and is exact enough at the scale of a city's route list.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

import geopandas as gpd
import numpy as np
import shapely
from shapely.geometry import LineString, Point
from sqlalchemy import Connection, text

from cluster import RESAMPLE_STEP_M, ROUTE_EPS_M, STOP_EPS_M, Network, resample_traces
from frechet import discrete_frechet

logger = logging.getLogger(__name__)

WGS84 = "EPSG:4326"
UTM36S = "EPSG:32736"

#: How far a route's canonical line may move and still be the same route.
#: Reuses the clustering radius: within it, two lines are close enough that
#: clustering itself would have called them one route.
ROUTE_NAME_EPS_M = ROUTE_EPS_M

#: How far a stop may move and still be the same stop. The clustering radius
#: again — beyond it, the dwells would have formed a different stop.
STOP_NAME_EPS_M = STOP_EPS_M


@dataclass(frozen=True)
class NamedRoute:
    """A route that carried a human-given name before the rebuild."""

    route_id: int
    short_name: str | None
    long_name: str | None
    geometry: LineString  # EPSG:32736

    @property
    def names(self) -> tuple[str | None, str | None]:
        return self.short_name, self.long_name

    def describe(self) -> str:
        return self.short_name or self.long_name or f"route {self.route_id}"


@dataclass(frozen=True)
class NamedStop:
    """A stop that carried a human-given name before the rebuild."""

    stop_id: int
    name: str
    centroid: Point  # EPSG:32736


# --------------------------------------------------------------------------
# Loading what was named
# --------------------------------------------------------------------------

_NAMED_ROUTES_SQL = text(
    """
    SELECT id, short_name, long_name, ST_AsText(geom) AS wkt
    FROM routes
    WHERE geom IS NOT NULL
      AND (COALESCE(short_name, '') <> '' OR COALESCE(long_name, '') <> '')
    ORDER BY id
    """
)

_NAMED_STOPS_SQL = text(
    """
    SELECT id, name, ST_AsText(geom) AS wkt
    FROM stops
    WHERE COALESCE(name, '') <> ''
    ORDER BY id
    """
)


def load_named_routes(conn: Connection) -> list[NamedRoute]:
    """Every route with a name, reprojected to EPSG:32736.

    Takes a `Connection` rather than an `Engine` so the read happens inside the
    same transaction as the rebuild that is about to delete these rows.
    """
    rows = conn.execute(_NAMED_ROUTES_SQL).mappings().all()
    if not rows:
        return []

    geometries = _to_utm([row["wkt"] for row in rows])
    return [
        NamedRoute(
            route_id=int(row["id"]),
            short_name=row["short_name"],
            long_name=row["long_name"],
            geometry=geometry,
        )
        for row, geometry in zip(rows, geometries, strict=True)
    ]


def load_named_stops(conn: Connection) -> list[NamedStop]:
    """Every stop with a name, reprojected to EPSG:32736."""
    rows = conn.execute(_NAMED_STOPS_SQL).mappings().all()
    if not rows:
        return []

    geometries = _to_utm([row["wkt"] for row in rows])
    return [
        NamedStop(stop_id=int(row["id"]), name=row["name"], centroid=centroid)
        for row, centroid in zip(rows, geometries, strict=True)
    ]


def _to_utm(wkts: Sequence[str]) -> list[LineString | Point]:
    return list(gpd.GeoSeries(shapely.from_wkt(list(wkts)), crs=WGS84).to_crs(UTM36S))


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------


def match_routes(
    new_geometries: Sequence[LineString],
    previous: Sequence[NamedRoute],
    *,
    eps_m: float = ROUTE_NAME_EPS_M,
    step_m: float = RESAMPLE_STEP_M,
) -> dict[int, NamedRoute]:
    """Map each new route's index to the named route it replaces, if any.

    Both sides are resampled to a uniform spacing first, for the reason
    `cluster.RESAMPLE_STEP_M` gives: discrete Fréchet compares *vertices*, so two
    identical corridors sampled differently score as far apart.
    """
    if not new_geometries or not previous:
        return {}

    fresh = resample_traces(list(new_geometries), step_m=step_m)
    old = resample_traces([route.geometry for route in previous], step_m=step_m)

    fresh_xy = [_coords(geometry) for geometry in fresh]
    old_xy = [_coords(geometry) for geometry in old]

    distances = np.full((len(fresh_xy), len(old_xy)), np.inf)
    for i, left in enumerate(fresh_xy):
        for j, right in enumerate(old_xy):
            # `max_distance` lets the Fréchet computation abandon a pair as soon
            # as it cannot come in under the threshold, which is most pairs.
            distances[i, j] = discrete_frechet(left, right, max_distance=eps_m)

    pairs = _greedy_pairs(distances, eps_m)
    return {index: previous[old_index] for index, old_index in pairs.items()}


def match_stops(
    new_centroids: Sequence[Point],
    previous: Sequence[NamedStop],
    *,
    eps_m: float = STOP_NAME_EPS_M,
) -> dict[int, NamedStop]:
    """Map each new stop's index to the named stop it replaces, if any.

    Plain distance between centroids, in metres — a stop is a point, so there is
    nothing for Fréchet to do here.
    """
    if not new_centroids or not previous:
        return {}

    fresh = np.array([[point.x, point.y] for point in new_centroids], dtype=float)
    old = np.array([[stop.centroid.x, stop.centroid.y] for stop in previous], dtype=float)

    distances = np.linalg.norm(fresh[:, None, :] - old[None, :, :], axis=2)

    pairs = _greedy_pairs(distances, eps_m)
    return {index: previous[old_index] for index, old_index in pairs.items()}


def _greedy_pairs(distances: np.ndarray, threshold: float) -> dict[int, int]:
    """One-to-one assignment of rows to columns, closest pairs first.

    Not the optimal assignment — that is the Hungarian algorithm, and it would
    be worth reaching for if these were large or if near-ties were common.
    Neither holds: a city has dozens of routes, and a pair that is genuinely the
    same route sits far below the threshold while everything else sits far
    above. Greedy is easier to reason about when a name lands somewhere
    surprising, which is the failure mode that matters.

    Ties break on the lower row index, then the lower column index, so a rerun
    over the same input assigns the same names.
    """
    candidates = [
        (float(distances[i, j]), i, j)
        for i in range(distances.shape[0])
        for j in range(distances.shape[1])
        if distances[i, j] <= threshold
    ]
    candidates.sort()

    taken_rows: set[int] = set()
    taken_columns: set[int] = set()
    pairs: dict[int, int] = {}

    for _distance, i, j in candidates:
        if i in taken_rows or j in taken_columns:
            continue
        pairs[i] = j
        taken_rows.add(i)
        taken_columns.add(j)

    return pairs


def _coords(geometry: LineString) -> np.ndarray:
    return np.asarray(geometry.coords, dtype=float)


# --------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CarriedNames:
    """Names to apply to a new clustering, keyed by cluster label.

    Labels are `RouteCluster.label` / `StopCluster.label` — identity within the
    run being persisted, which is all `persist_network` needs to insert with.
    """

    routes: dict[int, tuple[str | None, str | None]]
    stops: dict[int, str]

    def summary(self) -> str:
        return f"{len(self.routes)} route name(s) and {len(self.stops)} stop name(s) carried over"


def carry_over(
    network: Network,
    conn: Connection,
    *,
    route_eps_m: float = ROUTE_NAME_EPS_M,
    stop_eps_m: float = STOP_NAME_EPS_M,
) -> CarriedNames:
    """Work out which names in the database belong to which new clusters.

    Reads the existing named rows and matches them against `network`. Call it
    *before* deleting anything — it does not write, and it cannot recover names
    from rows that are already gone.
    """
    previous_routes = load_named_routes(conn)
    previous_stops = load_named_stops(conn)

    if not previous_routes and not previous_stops:
        return CarriedNames(routes={}, stops={})

    route_matches = match_routes(
        [route.canonical_geom for route in network.routes], previous_routes, eps_m=route_eps_m
    )
    stop_matches = match_stops(
        [stop.centroid for stop in network.stops], previous_stops, eps_m=stop_eps_m
    )

    routes = {
        network.routes[index].label: matched.names for index, matched in route_matches.items()
    }
    stops = {network.stops[index].label: matched.name for index, matched in stop_matches.items()}

    # Worth saying out loud: a name that finds no home is either a route that
    # has genuinely changed shape or one that has stopped being detected at all,
    # and both are things an operator should look at.
    carried_ids = {matched.route_id for matched in route_matches.values()}
    orphaned = [route for route in previous_routes if route.route_id not in carried_ids]
    if orphaned:
        logger.warning(
            "%d named route(s) matched nothing in the new clustering: %s",
            len(orphaned),
            ", ".join(route.describe() for route in orphaned),
        )

    logger.info("Carried %s.", CarriedNames(routes=routes, stops=stops).summary())
    return CarriedNames(routes=routes, stops=stops)
