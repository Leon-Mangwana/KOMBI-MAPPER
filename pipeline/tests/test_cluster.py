"""Tests for route and stop clustering.

No database: matched traces and dwell centroids are built directly in
EPSG:32736 from known corridors, so what the clustering *should* find is known
by construction. Corridors are laid out around the same Harare origin the
synthetic traces use, and every distance below is in metres.
"""

import geopandas as gpd
import numpy as np
import pytest
from shapely.geometry import LineString, Point

from clean import UTM36S
from cluster import (
    MAX_STOP_OFFSET_M,
    RESAMPLE_STEP_M,
    ROUTE_EPS_M,
    SIMPLIFY_TOLERANCE_M,
    STOP_EPS_M,
    STOP_MIN_SUPPORT,
    Network,
    RouteCluster,
    StopCluster,
    build_network,
    cluster_routes,
    cluster_stops,
    order_route_stops,
    pairwise_frechet,
    resample_traces,
    simplify_traces,
)
from frechet import discrete_frechet
from synthetic import DEFAULT_ORIGIN, DEFAULT_START

#: Two routes that share nothing but their first corner, plus a third for the
#: one-off uploads that should cluster with nothing.
CORRIDOR_A = ((0.0, 0.0), (2000.0, 0.0), (2000.0, 1500.0), (4000.0, 1500.0))
CORRIDOR_B = ((0.0, 0.0), (0.0, 2000.0), (1500.0, 2000.0), (1500.0, 4000.0))
CORRIDOR_C = ((-3000.0, -3000.0), (-3000.0, 1000.0))

#: A route that comes back down the other side of the same street. Ordering
#: stops on this one by anything other than distance *along* the line fails.
LOOP = ((0.0, 0.0), (2000.0, 0.0), (2000.0, 120.0), (0.0, 120.0))


def at(x: float, y: float) -> Point:
    """A point at an offset from the origin the corridors are laid out around."""
    return Point(DEFAULT_ORIGIN[0] + x, DEFAULT_ORIGIN[1] + y)


def trace_line(
    waypoints: tuple[tuple[float, float], ...],
    *,
    spacing_m: float = 40.0,
    trim_start_m: float = 0.0,
    trim_end_m: float = 0.0,
    jitter_m: float = 0.0,
    seed: int = 0,
) -> LineString:
    """A matched-trace-shaped line along `waypoints`.

    `trim_start_m` and `trim_end_m` model the ordinary case of a contributor
    boarding and alighting part-way along the route; `jitter_m` models the
    residual disagreement between two traces snapped to the same road.
    """
    path = LineString([(DEFAULT_ORIGIN[0] + x, DEFAULT_ORIGIN[1] + y) for x, y in waypoints])
    end = path.length - trim_end_m
    distances = np.arange(trim_start_m, end, spacing_m)
    distances = np.append(distances, end)

    xy = np.array([[point.x, point.y] for point in map(path.interpolate, distances)])
    if jitter_m:
        xy = xy + np.random.default_rng(seed).normal(0.0, jitter_m, size=xy.shape)
    return LineString(xy)


def traces_frame(lines: dict[int, LineString]) -> gpd.GeoDataFrame:
    """A `load_matched_traces`-shaped frame from `{trace_id: geometry}`."""
    return gpd.GeoDataFrame(
        {"trace_id": list(lines)},
        geometry=list(lines.values()),
        crs=UTM36S,
    )


def dwell_frame(rows: list[tuple[int, float, float, float]]) -> gpd.GeoDataFrame:
    """A `collect_dwells`-shaped frame from `(trace_id, x, y, duration_s)` rows."""
    return gpd.GeoDataFrame(
        {
            "trace_id": [int(trace_id) for trace_id, _, _, _ in rows],
            "duration_s": [float(duration) for _, _, _, duration in rows],
            "start_time": [DEFAULT_START] * len(rows),
        },
        geometry=[at(x, y) for _, x, y, _ in rows],
        crs=UTM36S,
    )


def dwells_around(
    trace_ids: tuple[int, ...],
    x: float,
    y: float,
    *,
    scatter_m: float = 8.0,
    duration_s: float = 30.0,
    seed: int = 0,
) -> list[tuple[int, float, float, float]]:
    """One dwell per trace, scattered a few metres about `(x, y)`."""
    rng = np.random.default_rng(seed)
    return [
        (
            trace_id,
            x + rng.normal(0.0, scatter_m),
            y + rng.normal(0.0, scatter_m),
            duration_s,
        )
        for trace_id in trace_ids
    ]


class TestSimplifyTraces:
    def test_a_straight_line_keeps_only_its_endpoints(self):
        dense = trace_line(((0.0, 0.0), (2000.0, 0.0)), spacing_m=10.0)
        assert len(dense.coords) > 100

        (simplified,) = simplify_traces([dense])

        assert len(simplified.coords) == 2

    def test_corners_survive(self):
        (simplified,) = simplify_traces([trace_line(CORRIDOR_A, spacing_m=10.0)])

        # The three straight legs collapse; the two corners between them do not.
        assert len(simplified.coords) == 4

    def test_endpoints_are_preserved_exactly(self):
        line = trace_line(CORRIDOR_A, spacing_m=25.0, jitter_m=3.0, seed=1)

        (simplified,) = simplify_traces([line])

        assert simplified.coords[0] == line.coords[0]
        assert simplified.coords[-1] == line.coords[-1]

    def test_no_vertex_moves_further_than_the_tolerance(self):
        line = trace_line(CORRIDOR_A, spacing_m=20.0, jitter_m=4.0, seed=2)

        (simplified,) = simplify_traces([line], SIMPLIFY_TOLERANCE_M)

        offsets = [simplified.distance(Point(xy)) for xy in line.coords]
        assert max(offsets) <= SIMPLIFY_TOLERANCE_M

    def test_a_larger_tolerance_drops_more(self):
        line = trace_line(CORRIDOR_A, spacing_m=20.0, jitter_m=6.0, seed=3)

        coarse = simplify_traces([line], 100.0)[0]
        fine = simplify_traces([line], 5.0)[0]

        assert len(coarse.coords) < len(fine.coords)

    def test_handles_an_empty_batch(self):
        assert simplify_traces([]) == []


class TestResampleTraces:
    def test_no_gap_exceeds_the_step(self):
        (resampled,) = resample_traces([trace_line(CORRIDOR_A)], 50.0)

        gaps = np.hypot(*np.diff(np.array(resampled.coords), axis=0).T)
        assert gaps.max() <= 50.0 + 1e-6

    def test_the_shape_is_unchanged(self):
        # Every new vertex lies on the line it came from, and the length is the
        # same: resampling redistributes vertices, it does not move the route.
        line = trace_line(CORRIDOR_A, spacing_m=25.0)

        (resampled,) = resample_traces([line])

        assert resampled.length == pytest.approx(line.length)
        assert max(line.distance(Point(xy)) for xy in resampled.coords) < 1e-6

    def test_endpoints_are_landed_on_exactly(self):
        line = trace_line(CORRIDOR_A, trim_start_m=137.0, trim_end_m=61.0)

        (resampled,) = resample_traces([line])

        assert Point(resampled.coords[0]).distance(Point(line.coords[0])) < 1e-6
        assert Point(resampled.coords[-1]).distance(Point(line.coords[-1])) < 1e-6

    def test_a_sparse_line_gains_vertices(self):
        (sparse,) = simplify_traces([trace_line(((0.0, 0.0), (2000.0, 0.0)))])
        assert len(sparse.coords) == 2

        (resampled,) = resample_traces([sparse], 50.0)

        assert len(resampled.coords) == 41

    def test_a_degenerate_line_is_left_alone(self):
        point = at(0.0, 0.0)
        stationary = LineString([point, point])

        (resampled,) = resample_traces([stationary])

        assert resampled.equals(stationary)

    def test_rejects_a_non_positive_step(self):
        with pytest.raises(ValueError, match="step_m must be positive"):
            resample_traces([trace_line(CORRIDOR_A)], 0.0)

    def test_handles_an_empty_batch(self):
        assert resample_traces([]) == []

    def test_it_is_what_makes_the_frechet_distance_usable(self):
        """The regression this step exists for. See `cluster.RESAMPLE_STEP_M`.

        These two traces of one corridor never diverge by more than about 10 m.
        Douglas–Peucker alone leaves each with a vertex the other has no partner
        for anywhere near — the legs are kilometres long and have no interior
        vertices — and the discrete Fréchet distance reads several hundred
        metres, far outside `ROUTE_EPS_M`. Resampling bounds that artefact at
        half a step.
        """
        lines = [trace_line(CORRIDOR_B, jitter_m=3.0, seed=40 + index) for index in range(2)]
        simplified = simplify_traces(lines)

        assert pairwise_frechet(simplified)[0, 1] > ROUTE_EPS_M
        assert pairwise_frechet(resample_traces(simplified))[0, 1] < RESAMPLE_STEP_M


class TestPairwiseFrechet:
    def test_is_symmetric_with_a_zero_diagonal(self):
        lines = [
            trace_line(CORRIDOR_A, jitter_m=3.0, seed=4),
            trace_line(CORRIDOR_B, jitter_m=3.0, seed=5),
            trace_line(CORRIDOR_C, jitter_m=3.0, seed=6),
        ]

        distances = pairwise_frechet(lines)

        assert distances.shape == (3, 3)
        np.testing.assert_allclose(distances, distances.T)
        np.testing.assert_allclose(np.diag(distances), 0.0)

    def test_entries_are_the_frechet_distance(self):
        lines = [trace_line(CORRIDOR_A), trace_line(CORRIDOR_A, trim_start_m=300.0)]

        distances = pairwise_frechet(lines)

        expected = discrete_frechet(np.array(lines[0].coords), np.array(lines[1].coords))
        assert distances[0, 1] == pytest.approx(expected)

    def test_traces_of_one_corridor_are_far_closer_than_of_two(self):
        same = [trace_line(CORRIDOR_A, jitter_m=3.0, seed=s) for s in (7, 8)]
        other = trace_line(CORRIDOR_B, jitter_m=3.0, seed=9)

        within = pairwise_frechet(same)[0, 1]
        across = pairwise_frechet([same[0], other])[0, 1]

        assert within < ROUTE_EPS_M < across

    def test_pruning_agrees_about_membership(self):
        lines = [
            trace_line(CORRIDOR_A, jitter_m=3.0, seed=10),
            trace_line(CORRIDOR_A, trim_start_m=250.0, seed=11),
            trace_line(CORRIDOR_B, jitter_m=3.0, seed=12),
            trace_line(CORRIDOR_C, seed=13),
        ]

        exact = pairwise_frechet(lines)
        pruned = pairwise_frechet(lines, max_distance=ROUTE_EPS_M)

        np.testing.assert_array_equal(exact <= ROUTE_EPS_M, pruned <= ROUTE_EPS_M)

    def test_an_empty_batch_gives_an_empty_matrix(self):
        assert pairwise_frechet([]).shape == (0, 0)

    def test_a_single_trace_gives_a_one_by_one_zero(self):
        distances = pairwise_frechet([trace_line(CORRIDOR_A)])

        assert distances.shape == (1, 1)
        assert distances[0, 0] == 0.0


class TestClusterRoutes:
    @pytest.fixture()
    def two_corridors(self):
        """Three traces of corridor A, three of B, and one lone ride down C."""
        lines = {}
        for index, seed in enumerate((20, 21, 22)):
            lines[10 + index] = trace_line(
                CORRIDOR_A, jitter_m=3.0, trim_start_m=60.0 * index, seed=seed
            )
        for index, seed in enumerate((23, 24, 25)):
            lines[20 + index] = trace_line(
                CORRIDOR_B, jitter_m=3.0, trim_end_m=60.0 * index, seed=seed
            )
        lines[30] = trace_line(CORRIDOR_C, jitter_m=3.0, seed=26)
        return traces_frame(lines)

    def test_finds_one_route_per_corridor(self, two_corridors):
        routes = cluster_routes(two_corridors)

        assert len(routes) == 2
        assert {route.trace_ids for route in routes} == {(10, 11, 12), (20, 21, 22)}

    def test_an_uncorroborated_trace_joins_no_route(self, two_corridors):
        routes = cluster_routes(two_corridors)

        clustered = {trace_id for route in routes for trace_id in route.trace_ids}
        assert 30 not in clustered

    def test_the_medoid_is_a_member_of_its_own_cluster(self, two_corridors):
        for route in cluster_routes(two_corridors):
            assert route.medoid_trace_id in route.trace_ids

    def test_the_medoid_is_the_most_central_trace(self):
        # Three traces of one corridor, boarded 0 m, 120 m and 240 m along. The
        # middle one is 120 m from each of the others; the outer two are 240 m
        # apart, so either of them would total 360 m against the middle's 240 m.
        traces = traces_frame(
            {
                1: trace_line(CORRIDOR_A, trim_start_m=0.0),
                2: trace_line(CORRIDOR_A, trim_start_m=120.0),
                3: trace_line(CORRIDOR_A, trim_start_m=240.0),
            }
        )

        (route,) = cluster_routes(traces)

        assert route.trace_ids == (1, 2, 3)
        assert route.medoid_trace_id == 2

    def test_pruning_does_not_move_the_medoid(self):
        """Trace 1 and trace 3 above are 240 m apart — beyond `eps` — and are in
        one cluster only because trace 2 chains them. Their distance is exactly
        the entry pruning replaces with a bound, so the medoid has to be chosen
        from recomputed values."""
        traces = traces_frame(
            {
                1: trace_line(CORRIDOR_A, trim_start_m=0.0),
                2: trace_line(CORRIDOR_A, trim_start_m=120.0),
                3: trace_line(CORRIDOR_A, trim_start_m=240.0),
            }
        )

        pruned = cluster_routes(traces, prune=True)
        exact = cluster_routes(traces, prune=False)

        assert [route.medoid_trace_id for route in pruned] == [
            route.medoid_trace_id for route in exact
        ]
        assert [route.trace_ids for route in pruned] == [route.trace_ids for route in exact]

    def test_canonical_geom_is_the_medoid_at_full_resolution(self, two_corridors):
        (route, _) = cluster_routes(two_corridors)

        original = two_corridors.loc[
            two_corridors["trace_id"] == route.medoid_trace_id, "geometry"
        ].iloc[0]
        assert route.canonical_geom.equals(original)
        # Not the 15 m-simplified line the comparison ran on: that would ship
        # cut corners to the map.
        assert len(route.canonical_geom.coords) > 10

    def test_a_trace_driven_the_other_way_is_not_the_same_route(self):
        """The reason for Fréchet over Hausdorff, on real geometry: reversing a
        trace leaves the point set identical and the route emphatically not."""
        forward = trace_line(CORRIDOR_A, jitter_m=2.0, seed=27)
        traces = traces_frame(
            {
                1: forward,
                2: trace_line(CORRIDOR_A, jitter_m=2.0, seed=28),
                3: LineString(list(forward.coords)[::-1]),
            }
        )

        (route,) = cluster_routes(traces)

        assert route.trace_ids == (1, 2)

    def test_routes_come_back_ordered_by_support(self):
        lines = {index: trace_line(CORRIDOR_A, jitter_m=3.0, seed=30 + index) for index in range(4)}
        lines.update(
            {
                10 + index: trace_line(CORRIDOR_B, jitter_m=3.0, seed=40 + index)
                for index in range(2)
            }
        )

        routes = cluster_routes(traces_frame(lines))

        assert [route.support for route in routes] == [4, 2]

    def test_min_traces_is_respected(self):
        traces = traces_frame(
            {
                1: trace_line(CORRIDOR_A, jitter_m=3.0, seed=50),
                2: trace_line(CORRIDOR_A, jitter_m=3.0, seed=51),
            }
        )

        assert len(cluster_routes(traces, min_traces=2)) == 1
        assert cluster_routes(traces, min_traces=3) == []

    def test_a_tighter_eps_splits_a_corridor_by_boarding_point(self):
        traces = traces_frame(
            {
                1: trace_line(CORRIDOR_A, trim_start_m=0.0),
                2: trace_line(CORRIDOR_A, trim_start_m=30.0),
                3: trace_line(CORRIDOR_A, trim_start_m=900.0),
                4: trace_line(CORRIDOR_A, trim_start_m=930.0),
            }
        )

        assert len(cluster_routes(traces, eps_m=1000.0)) == 1
        assert len(cluster_routes(traces, eps_m=100.0)) == 2

    def test_labels_are_unique(self, two_corridors):
        routes = cluster_routes(two_corridors)

        assert len({route.label for route in routes}) == len(routes)

    def test_support_and_length_are_reported(self, two_corridors):
        route = cluster_routes(two_corridors)[0]

        assert route.support == len(route.trace_ids) == 3
        assert route.length_m == pytest.approx(route.canonical_geom.length)

    def test_no_traces_gives_no_routes(self):
        assert cluster_routes(traces_frame({})) == []


class TestClusterStops:
    def test_three_traces_dwelling_together_make_a_stop(self):
        dwells = dwell_frame(dwells_around((1, 2, 3), 1000.0, 0.0, seed=60))

        (stop,) = cluster_stops(dwells)

        assert stop.support == 3
        assert stop.dwell_count == 3
        assert stop.centroid.distance(at(1000.0, 0.0)) < STOP_EPS_M

    def test_two_traces_are_not_enough(self):
        dwells = dwell_frame(dwells_around((1, 2), 1000.0, 0.0, seed=61))

        assert cluster_stops(dwells) == []

    def test_repeated_dwells_from_one_trace_are_one_observation(self):
        """Three dwells, one ride: a kombi crawling through a robot queue, not a
        stop three contributors have confirmed."""
        rows = [(1, 1000.0 + offset, 0.0, 25.0) for offset in (-6.0, 0.0, 6.0)]

        assert cluster_stops(dwell_frame(rows)) == []

    def test_two_traces_dwelling_twice_each_are_still_two_traces(self):
        rows = [
            (trace_id, 1000.0 + offset, 0.0, 25.0) for trace_id in (1, 2) for offset in (-5.0, 5.0)
        ]

        assert cluster_stops(dwell_frame(rows)) == []
        # ...but the same four dwells do pass a threshold of two.
        assert len(cluster_stops(dwell_frame(rows), min_support=2)) == 1

    def test_a_trace_that_passes_twice_raises_the_dwell_count_not_the_support(self):
        rows = dwells_around((1, 2, 3), 1000.0, 0.0, seed=62)
        rows.append((1, 1004.0, 3.0, 40.0))  # outbound and inbound, same kerb

        (stop,) = cluster_stops(dwell_frame(rows))

        assert stop.support == 3
        assert stop.dwell_count == 4
        assert stop.trace_ids == (1, 2, 3)

    def test_separate_places_give_separate_stops(self):
        rows = dwells_around((1, 2, 3), 0.0, 0.0, seed=63)
        rows += dwells_around((1, 2, 3), 400.0, 0.0, seed=64)

        stops = cluster_stops(dwell_frame(rows))

        assert len(stops) == 2
        centres = sorted(stop.centroid.x for stop in stops)
        assert centres[1] - centres[0] == pytest.approx(400.0, abs=20.0)

    def test_dwells_further_apart_than_eps_do_not_join(self):
        # One dwell each from three traces, but 100 m apart: three places nobody
        # has confirmed, not one stop.
        rows = [(index + 1, 100.0 * index, 0.0, 30.0) for index in range(3)]

        assert cluster_stops(dwell_frame(rows)) == []

    def test_eps_is_in_metres(self):
        rows = [(index + 1, 60.0 * index, 0.0, 30.0) for index in range(3)]

        assert cluster_stops(dwell_frame(rows), eps_m=30.0) == []
        assert len(cluster_stops(dwell_frame(rows), eps_m=70.0)) == 1

    def test_reports_the_median_dwell(self):
        rows = [
            (1, 1000.0, 0.0, 20.0),
            (2, 1003.0, 0.0, 30.0),
            (3, 997.0, 0.0, 130.0),
        ]

        (stop,) = cluster_stops(dwell_frame(rows))

        assert stop.median_dwell_s == pytest.approx(30.0)

    def test_stops_come_back_ordered_by_support(self):
        rows = dwells_around((1, 2, 3, 4, 5), 0.0, 0.0, seed=65)
        rows += dwells_around((1, 2, 3), 500.0, 0.0, seed=66)

        assert [stop.support for stop in cluster_stops(dwell_frame(rows))] == [5, 3]

    def test_no_dwells_gives_no_stops(self):
        assert cluster_stops(dwell_frame([])) == []

    def test_the_documented_defaults(self):
        assert STOP_EPS_M == 40.0
        assert STOP_MIN_SUPPORT == 3


class TestOrderRouteStops:
    def route(self, waypoints=CORRIDOR_A, *, trace_ids=(1, 2, 3), label=0) -> RouteCluster:
        line = trace_line(waypoints, spacing_m=25.0)
        return RouteCluster(
            label=label,
            trace_ids=trace_ids,
            medoid_trace_id=trace_ids[0],
            canonical_geom=line,
        )

    def stop(self, label: int, x: float, y: float, trace_ids=(1, 2, 3)) -> StopCluster:
        return StopCluster(
            label=label,
            centroid=at(x, y),
            trace_ids=trace_ids,
            dwell_count=len(trace_ids),
            median_dwell_s=30.0,
        )

    def test_orders_stops_along_the_route_not_by_input_order(self):
        route = self.route()
        stops = [
            self.stop(0, 2000.0, 1200.0),  # ~3200 m along
            self.stop(1, 500.0, 0.0),  # 500 m along
            self.stop(2, 2000.0, 400.0),  # ~2400 m along
        ]

        (ordered,) = order_route_stops([route], stops)

        assert ordered.stop_labels == (1, 2, 0)

    def test_a_route_that_doubles_back_orders_by_distance_travelled(self):
        """The case that decides the method. The route runs 2 km east, crosses
        the street and comes back; a stop 200 m from the start on the return leg
        is the *last* stop, not the first. Ordering by distance from the origin,
        or by nearest-neighbour chaining, gets this backwards."""
        route = self.route(LOOP)
        outbound = self.stop(0, 1800.0, 0.0)  # 1800 m along
        inbound = self.stop(1, 200.0, 120.0)  # ~3920 m along

        (ordered,) = order_route_stops([route], [inbound, outbound])

        assert ordered.stop_labels == (0, 1)
        assert inbound.centroid.distance(at(0.0, 0.0)) < outbound.centroid.distance(at(0.0, 0.0))

    def test_both_legs_of_a_loop_keep_their_own_stops(self):
        route = self.route(LOOP)
        stops = [
            self.stop(0, 500.0, 0.0),
            self.stop(1, 1500.0, 0.0),
            self.stop(2, 1500.0, 120.0),
            self.stop(3, 500.0, 120.0),
        ]

        (ordered,) = order_route_stops([route], stops)

        assert ordered.stop_labels == (0, 1, 2, 3)

    def test_a_stop_off_the_route_is_not_attributed_to_it(self):
        route = self.route()
        stops = [self.stop(0, 500.0, 0.0), self.stop(1, 500.0, 900.0)]

        (ordered,) = order_route_stops([route], stops)

        assert ordered.stop_labels == (0,)

    def test_the_offset_limit_is_configurable(self):
        route = self.route()
        stops = [self.stop(0, 500.0, 200.0)]

        assert order_route_stops([route], stops)[0].stop_labels == ()
        assert order_route_stops([route], stops, max_offset_m=300.0)[0].stop_labels == (0,)

    def test_a_stop_none_of_the_routes_traces_visited_is_skipped(self):
        route = self.route(trace_ids=(1, 2, 3))
        stops = [self.stop(0, 500.0, 0.0, trace_ids=(7, 8, 9))]

        (ordered,) = order_route_stops([route], stops)

        assert ordered.stop_labels == ()

    def test_one_shared_trace_is_enough_to_attribute_a_stop(self):
        route = self.route(trace_ids=(1, 2, 3))
        stops = [self.stop(0, 500.0, 0.0, trace_ids=(3, 8, 9))]

        assert order_route_stops([route], stops)[0].stop_labels == (0,)

    def test_a_stop_can_serve_two_routes(self):
        shared = self.stop(0, 0.0, 0.0, trace_ids=(1, 2, 5))
        routes = [
            self.route(CORRIDOR_A, trace_ids=(1, 2, 3), label=0),
            self.route(CORRIDOR_B, trace_ids=(4, 5, 6), label=1),
        ]

        ordered = order_route_stops(routes, [shared])

        assert all(route.stop_labels == (0,) for route in ordered)

    def test_the_input_routes_are_left_alone(self):
        route = self.route()

        order_route_stops([route], [self.stop(0, 500.0, 0.0)])

        assert route.stop_labels == ()

    def test_a_route_with_no_stops_survives_with_an_empty_list(self):
        (ordered,) = order_route_stops([self.route()], [])

        assert ordered.stop_labels == ()
        assert ordered.canonical_geom.equals(self.route().canonical_geom)

    def test_the_documented_offset_default(self):
        assert MAX_STOP_OFFSET_M == 60.0


class TestBuildNetwork:
    @pytest.fixture()
    def network(self) -> Network:
        traces = traces_frame(
            {
                1: trace_line(CORRIDOR_A, jitter_m=3.0, seed=70),
                2: trace_line(CORRIDOR_A, jitter_m=3.0, seed=71),
                3: trace_line(CORRIDOR_A, jitter_m=3.0, seed=72),
                4: trace_line(CORRIDOR_B, jitter_m=3.0, seed=73),
                5: trace_line(CORRIDOR_B, jitter_m=3.0, seed=74),
                6: trace_line(CORRIDOR_B, jitter_m=3.0, seed=75),
            }
        )
        rows = dwells_around((1, 2, 3), 1500.0, 0.0, seed=76)
        rows += dwells_around((1, 2, 3), 2000.0, 900.0, seed=77)
        rows += dwells_around((4, 5, 6), 0.0, 1200.0, seed=78)
        # Three riders of corridor A who all stopped somewhere far off it — a
        # real cluster of dwells, but not on any route we know about.
        rows += dwells_around((1, 2, 3), -2500.0, -2500.0, seed=79)

        return build_network(traces, dwell_frame(rows))

    def test_finds_both_routes(self, network):
        assert len(network.routes) == 2
        assert {route.trace_ids for route in network.routes} == {(1, 2, 3), (4, 5, 6)}

    def test_attaches_each_stop_to_the_route_that_passes_it(self, network):
        by_traces = {route.trace_ids: route for route in network.routes}

        assert len(by_traces[(1, 2, 3)].stop_labels) == 2
        assert len(by_traces[(4, 5, 6)].stop_labels) == 1

    def test_orders_the_stops_along_the_route(self, network):
        route = next(r for r in network.routes if r.trace_ids == (1, 2, 3))
        stops = network.stops_by_label

        along = [route.canonical_geom.project(stops[label].centroid) for label in route.stop_labels]
        assert along == sorted(along)

    def test_drops_the_stop_no_route_passes(self, network):
        assert len(network.stops) == 3
        for stop in network.stops:
            assert stop.centroid.distance(at(-2500.0, -2500.0)) > 1000.0

    def test_stops_are_held_once_and_referenced_by_label(self, network):
        labels = [label for route in network.routes for label in route.stop_labels]

        assert sorted(labels) == sorted(stop.label for stop in network.stops)
        assert len(network.stops_by_label) == len(network.stops)

    def test_summarises_itself(self, network):
        assert network.summary() == ("2 route(s) over 6 trace(s); 3 stop(s), 3 route-stop link(s)")

    def test_empty_input_gives_an_empty_network(self):
        empty = build_network(traces_frame({}), dwell_frame([]))

        assert empty.routes == ()
        assert empty.stops == ()

    def test_traces_with_no_dwells_still_give_routes(self):
        traces = traces_frame(
            {index: trace_line(CORRIDOR_A, jitter_m=3.0, seed=80 + index) for index in range(3)}
        )

        result = build_network(traces, dwell_frame([]))

        assert len(result.routes) == 1
        assert result.stops == ()
        assert result.routes[0].stop_labels == ()


# --------------------------------------------------------------------------
# Dwell collection
# --------------------------------------------------------------------------


def test_collect_dwells_skips_a_trace_cleaning_rejects(monkeypatch, caplog):
    """One upload with broken timestamps must not cost the whole network its stops.

    `process.py` would have rejected such a trace, but a `matched_geom` can
    arrive by other routes (`seed_demo.py --fake-match`), and clustering
    re-cleans from the raw points regardless.
    """
    import logging

    import cluster

    monkeypatch.setattr(cluster, "load_trace", lambda trace_id, engine=None: trace_id)

    def clean(trace_id):
        if trace_id == 2:
            raise ValueError("Trace has points with no timestamp; cannot derive speeds.")
        return trace_id

    monkeypatch.setattr(cluster, "clean_trace", clean)
    monkeypatch.setattr(
        cluster,
        "detect_dwells",
        lambda cleaned, **kwargs: [
            cluster.Dwell(
                centroid=Point(float(cleaned), 0.0),
                start_time=DEFAULT_START,
                duration_s=60.0,
                start_index=0,
                end_index=1,
            )
        ],
    )

    with caplog.at_level(logging.WARNING):
        dwells = cluster.collect_dwells([1, 2, 3], engine=object())

    assert list(dwells["trace_id"]) == [1, 3]
    assert "Trace 2 skipped" in caplog.text
