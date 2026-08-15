"""Tests for carrying names across a re-clustering run.

Names are the only data in the system nobody can recompute, so the thing under
test is: does a route that is still the same route keep its name, and does one
that is not fail to steal someone else's?

No database — the matching is pure, and `carry_over`'s two SQL reads are thin.
"""

import numpy as np
import pytest
from shapely.geometry import LineString, Point

from naming import NamedRoute, NamedStop, _greedy_pairs, match_routes, match_stops


def corridor(offset_m: float = 0.0, *, length_m: float = 3000.0, points: int = 40) -> LineString:
    """A straight east-west line, optionally shifted north by `offset_m`."""
    xs = np.linspace(0.0, length_m, points)
    return LineString([(x, offset_m) for x in xs])


def named(route_id: int, geometry: LineString, short: str | None = "A") -> NamedRoute:
    return NamedRoute(route_id=route_id, short_name=short, long_name=None, geometry=geometry)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


def test_an_unchanged_route_keeps_its_name():
    previous = [named(7, corridor())]

    matched = match_routes([corridor()], previous)

    assert matched[0].route_id == 7


def test_a_route_that_shifted_slightly_keeps_its_name():
    """More traces move a canonical line by metres, not hundreds of metres."""
    previous = [named(7, corridor())]

    matched = match_routes([corridor(offset_m=25.0)], previous)

    assert matched[0].route_id == 7


def test_a_route_that_moved_too_far_is_left_unnamed():
    """Beyond the radius it is a different corridor, and should be reviewed."""
    previous = [named(7, corridor())]

    matched = match_routes([corridor(offset_m=500.0)], previous, eps_m=200.0)

    assert matched == {}


def test_each_new_route_takes_the_nearest_name():
    previous = [named(1, corridor(0.0), short="North"), named(2, corridor(400.0), short="South")]

    matched = match_routes([corridor(390.0), corridor(10.0)], previous)

    assert matched[0].short_name == "South"
    assert matched[1].short_name == "North"


def test_two_similar_routes_cannot_share_one_name():
    """A one-to-one assignment: the closer takes it, the other is reviewed."""
    previous = [named(1, corridor(0.0))]

    matched = match_routes([corridor(20.0), corridor(60.0)], previous)

    assert list(matched) == [0], "only the closer route is named"
    assert matched[0].route_id == 1


def test_a_reversed_route_does_not_inherit_the_name():
    """Inbound and outbound are separate routes with separate names.

    Fréchet is direction-sensitive, and matching a reversed geometry would hand
    each direction the other's name.
    """
    previous = [named(7, corridor())]
    reversed_geometry = LineString(list(corridor().coords)[::-1])

    matched = match_routes([reversed_geometry], previous, eps_m=200.0)

    assert matched == {}


def test_matching_survives_a_change_in_vertex_spacing():
    """The same corridor sampled differently is still the same corridor.

    Discrete Fréchet compares vertices, so without the resampling both sides get
    this would score two identical lines as far apart.
    """
    previous = [named(7, corridor(points=8))]

    matched = match_routes([corridor(points=400)], previous)

    assert matched[0].route_id == 7


def test_no_previous_names_matches_nothing():
    assert match_routes([corridor()], []) == {}


def test_no_new_routes_matches_nothing():
    assert match_routes([], [named(1, corridor())]) == {}


def test_both_name_fields_are_carried():
    previous = [
        NamedRoute(route_id=3, short_name="4", long_name="CBD - Mbare", geometry=corridor())
    ]

    matched = match_routes([corridor()], previous)

    assert matched[0].names == ("4", "CBD - Mbare")


# --------------------------------------------------------------------------
# Stops
# --------------------------------------------------------------------------


def test_a_stop_that_barely_moved_keeps_its_name():
    previous = [NamedStop(stop_id=5, name="Copacabana", centroid=Point(0.0, 0.0))]

    matched = match_stops([Point(12.0, 9.0)], previous)

    assert matched[0].name == "Copacabana"


def test_a_stop_beyond_the_radius_is_left_unnamed():
    previous = [NamedStop(stop_id=5, name="Copacabana", centroid=Point(0.0, 0.0))]

    matched = match_stops([Point(300.0, 0.0)], previous, eps_m=40.0)

    assert matched == {}


def test_each_stop_takes_the_nearest_name():
    previous = [
        NamedStop(stop_id=1, name="Copacabana", centroid=Point(0.0, 0.0)),
        NamedStop(stop_id=2, name="Fourth Street", centroid=Point(100.0, 0.0)),
    ]

    matched = match_stops([Point(98.0, 0.0), Point(3.0, 0.0)], previous, eps_m=40.0)

    assert matched[0].name == "Fourth Street"
    assert matched[1].name == "Copacabana"


def test_two_stops_cannot_share_one_name():
    previous = [NamedStop(stop_id=1, name="Copacabana", centroid=Point(0.0, 0.0))]

    matched = match_stops([Point(5.0, 0.0), Point(10.0, 0.0)], previous, eps_m=40.0)

    assert list(matched) == [0]


# --------------------------------------------------------------------------
# The assignment itself
# --------------------------------------------------------------------------


def test_closest_pairs_are_assigned_first():
    """Greedy over ascending distance, so a near-perfect pair is never displaced."""
    distances = np.array(
        [
            [10.0, 1.0],
            [2.0, 3.0],
        ]
    )

    assert _greedy_pairs(distances, threshold=100.0) == {0: 1, 1: 0}


def test_pairs_over_the_threshold_are_not_assigned():
    distances = np.array([[5.0, 90.0]])

    assert _greedy_pairs(distances, threshold=50.0) == {0: 0}


def test_assignment_is_one_to_one():
    distances = np.array([[1.0], [2.0], [3.0]])

    assert _greedy_pairs(distances, threshold=100.0) == {0: 0}


def test_ties_break_deterministically():
    """A rerun over the same input must assign the same names."""
    distances = np.array([[5.0, 5.0], [5.0, 5.0]])

    first = _greedy_pairs(distances, threshold=100.0)
    second = _greedy_pairs(distances, threshold=100.0)

    assert first == second == {0: 0, 1: 1}


def test_the_threshold_is_inclusive():
    assert _greedy_pairs(np.array([[50.0]]), threshold=50.0) == {0: 0}


@pytest.mark.parametrize("shape", [(0, 3), (3, 0), (0, 0)])
def test_empty_inputs_assign_nothing(shape):
    assert _greedy_pairs(np.empty(shape), threshold=10.0) == {}
