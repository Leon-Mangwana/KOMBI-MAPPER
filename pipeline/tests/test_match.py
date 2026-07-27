"""Tests for map matching.

No OSRM instance is needed. `fake_osrm` stands in for one: it snaps whatever
coordinates it is asked about onto a known road and answers in OSRM's response
shape. Because the road is the same polyline `synthetic_trace` generates from,
the correct matched output is known exactly — it is the road — and the tests
assert against that rather than against a recorded response.

The fake is deliberately literal about the parts the stitching depends on: one
`tracepoints` entry per input coordinate, `matchings_index` on each, and
geometry that runs between the snapped locations of a matching's first and last
point. Its `unmatched_requests` and `null_range` options reproduce the two ways
OSRM declines to match — a whole request coming back `NoMatch`, and a hole
inside an otherwise fine response.
"""

import json
import logging

import geopandas as gpd
import httpx
import numpy as np
import pytest
from shapely.geometry import LineString, Point
from shapely.ops import substring

from clean import UTM36S, WGS84, clean_trace
from match import (
    MAX_COORDINATES_PER_REQUEST,
    MatchResult,
    OSRMError,
    _plan_chunks,
    match_trace,
)
from synthetic import DEFAULT_ORIGIN, DEFAULT_WAYPOINTS, synthetic_trace

OSRM_URL = "http://osrm.test"

#: The road the synthetic traces are generated along, in EPSG:32736.
ROAD = LineString(
    [(DEFAULT_ORIGIN[0] + wx, DEFAULT_ORIGIN[1] + wy) for wx, wy in DEFAULT_WAYPOINTS]
)


def to_wgs84(points: list[Point]) -> list[tuple[float, float]]:
    series = gpd.GeoSeries(points, crs=UTM36S).to_crs(WGS84)
    return [(float(p.x), float(p.y)) for p in series]


def to_metric(lonlats: list[tuple[float, float]]) -> list[Point]:
    lons = [lon for lon, _ in lonlats]
    lats = [lat for _, lat in lonlats]
    series = gpd.GeoSeries(gpd.points_from_xy(lons, lats), crs=WGS84).to_crs(UTM36S)
    return list(series)


def parse_coordinates(request: httpx.Request) -> list[tuple[float, float]]:
    """Pull the `lon,lat;lon,lat` list back out of a match URL."""
    _, _, tail = request.url.path.partition("/match/v1/driving/")
    return [tuple(float(part) for part in pair.split(",")) for pair in tail.split(";")]


def fake_osrm(
    *,
    road: LineString = ROAD,
    unmatched_requests: tuple[int, ...] = (),
    null_range: tuple[int, int] | None = None,
    confidence: float = 0.9,
):
    """An OSRM stand-in that snaps coordinates onto `road`.

    Args:
        unmatched_requests: Ordinals of requests to answer `NoMatch` to.
        null_range: Request-relative `(first, last)` indices to return `null`
            tracepoints for, splitting the response into two matchings either
            side. Applied to every matched request.
        confidence: What to report for each matching.

    Returns:
        `(handler, requests)` — the transport handler, and a list that
        accumulates every `httpx.Request` it saw.
    """
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        ordinal = len(requests)
        requests.append(request)

        if ordinal in unmatched_requests:
            return httpx.Response(
                400, json={"code": "NoMatch", "message": "Could not match the trace."}
            )

        coordinates = parse_coordinates(request)
        distances = [road.project(point) for point in to_metric(coordinates)]

        if null_range is None:
            groups = [list(range(len(coordinates)))]
            nulls: set[int] = set()
        else:
            first, last = null_range
            nulls = set(range(first, last + 1))
            groups = [
                [i for i in range(0, first)],
                [i for i in range(last + 1, len(coordinates))],
            ]
        groups = [group for group in groups if len(group) >= 2]

        matchings = []
        assignment: dict[int, int] = {}
        for matching_index, group in enumerate(groups):
            piece = substring(road, distances[group[0]], distances[group[-1]])
            matchings.append(
                {
                    "confidence": confidence,
                    "distance": piece.length,
                    "duration": piece.length / 10.0,
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [
                            list(coordinate)
                            for coordinate in to_wgs84([Point(c) for c in piece.coords])
                        ],
                    },
                    "legs": [],
                }
            )
            for index in group:
                assignment[index] = matching_index

        snapped = to_wgs84([road.interpolate(distance) for distance in distances])
        waypoint_counter: dict[int, int] = {}
        tracepoints: list[dict | None] = []
        for index in range(len(coordinates)):
            if index in nulls or index not in assignment:
                tracepoints.append(None)
                continue
            matching_index = assignment[index]
            waypoint_index = waypoint_counter.get(matching_index, 0)
            waypoint_counter[matching_index] = waypoint_index + 1
            tracepoints.append(
                {
                    "location": list(snapped[index]),
                    "matchings_index": matching_index,
                    "waypoint_index": waypoint_index,
                    "alternatives_count": 0,
                    "name": "Samora Machel Ave",
                }
            )

        return httpx.Response(
            200, json={"code": "Ok", "matchings": matchings, "tracepoints": tracepoints}
        )

    return handler, requests


def run_match(cleaned, handler, **kwargs) -> MatchResult:
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        return match_trace(cleaned, osrm_url=OSRM_URL, client=client, **kwargs)


def max_offset_from_road(geometry: LineString) -> float:
    """Furthest any vertex of `geometry` sits from the road, in metres."""
    return max(ROAD.distance(Point(coordinate)) for coordinate in geometry.coords)


@pytest.fixture()
def short_trace():
    """A 60 s trace — one chunk, comfortably under the coordinate cap."""
    return clean_trace(synthetic_trace(duration_s=60.0, noise_sigma_m=4.0, seed=10).noisy)


@pytest.fixture()
def long_trace():
    """A 400 s trace — 401 points, so several overlapping chunks."""
    return clean_trace(synthetic_trace(duration_s=400.0, noise_sigma_m=4.0, seed=11).noisy)


class TestPlanChunks:
    def test_short_traces_are_one_request(self):
        chunks = _plan_chunks(40, max_coordinates=100, overlap=10)

        assert len(chunks) == 1
        assert (chunks[0].context_start, chunks[0].context_end) == (0, 39)
        assert (chunks[0].owned_start, chunks[0].owned_end) == (0, 39)

    def test_owned_ranges_cover_the_trace_sharing_only_their_seams(self):
        chunks = _plan_chunks(401, max_coordinates=100, overlap=10)

        covered: list[int] = []
        for chunk in chunks:
            # Consecutive chunks share the seam point deliberately: both cut
            # their geometry there, so the two pieces meet.
            assert not covered or chunk.owned_start == covered[-1]
            covered.extend(
                range(
                    chunk.owned_start if not covered else chunk.owned_start + 1, chunk.owned_end + 1
                )
            )
        assert covered == list(range(401))

    def test_no_request_exceeds_the_coordinate_cap(self):
        for count in (101, 250, 401, 1000):
            for chunk in _plan_chunks(count, max_coordinates=100, overlap=10):
                assert chunk.context_end - chunk.context_start + 1 <= 100

    def test_owned_range_always_sits_inside_the_context(self):
        for chunk in _plan_chunks(1000, max_coordinates=100, overlap=10):
            assert chunk.context_start <= chunk.owned_start
            assert chunk.owned_end <= chunk.context_end

    def test_the_seam_falls_in_the_middle_of_the_overlap(self):
        first, second = _plan_chunks(180, max_coordinates=100, overlap=20)[:2]

        # Chunks overlap on 80..99; the seam is the middle of that, so each side
        # keeps only the half it had context for.
        assert (second.context_start, first.context_end) == (80, 99)
        assert first.owned_end == 90
        assert second.owned_start == 90

    def test_the_last_chunk_is_not_left_as_a_short_tail(self):
        chunks = _plan_chunks(185, max_coordinates=100, overlap=10)

        assert chunks[-1].context_end - chunks[-1].context_start + 1 == 100
        assert chunks[-1].context_end == 184

    def test_zero_overlap_still_covers_the_trace(self):
        chunks = _plan_chunks(250, max_coordinates=100, overlap=0)

        assert chunks[0].owned_start == 0
        assert chunks[-1].owned_end == 249
        for earlier, later in zip(chunks, chunks[1:], strict=False):
            assert later.owned_start == earlier.owned_end


class TestRequests:
    def test_a_short_trace_is_a_single_request(self, short_trace):
        handler, requests = fake_osrm()

        run_match(short_trace, handler)

        assert len(requests) == 1

    def test_a_long_trace_is_chunked_within_the_cap(self, long_trace):
        handler, requests = fake_osrm()

        run_match(long_trace, handler)

        assert len(requests) > 1
        for request in requests:
            assert len(parse_coordinates(request)) <= MAX_COORDINATES_PER_REQUEST

    def test_consecutive_chunks_overlap(self, long_trace):
        handler, requests = fake_osrm()

        run_match(long_trace, handler, max_coordinates=100, overlap=10)

        first = parse_coordinates(requests[0])
        second = parse_coordinates(requests[1])
        assert first[-10:] == second[:10]

    def test_sends_a_radius_per_point_from_the_reported_accuracy(self, short_trace):
        handler, requests = fake_osrm()

        run_match(short_trace, handler)

        radiuses = requests[0].url.params["radiuses"].split(";")
        assert len(radiuses) == len(parse_coordinates(requests[0]))
        # The fixture reports 4 m accuracy, which the 5 m floor lifts.
        assert {float(radius) for radius in radiuses} == {5.0}

    def test_clamps_the_radius_to_the_ceiling(self):
        cleaned = clean_trace(
            synthetic_trace(duration_s=30.0, reported_accuracy_m=48.0, seed=12).noisy
        )
        handler, requests = fake_osrm()

        run_match(cleaned, handler, max_radius_m=20.0)

        assert {float(r) for r in requests[0].url.params["radiuses"].split(";")} == {20.0}

    def test_sends_strictly_increasing_timestamps(self, short_trace):
        handler, requests = fake_osrm()

        run_match(short_trace, handler)

        timestamps = [int(t) for t in requests[0].url.params["timestamps"].split(";")]
        assert len(timestamps) == len(parse_coordinates(requests[0]))
        assert all(np.diff(timestamps) > 0)

    def test_omits_timestamps_that_would_not_be_strictly_increasing(self):
        # 4 Hz sampling collapses to duplicate seconds once rounded, and OSRM
        # rejects those outright — better to match without them.
        cleaned = clean_trace(synthetic_trace(duration_s=20.0, sample_hz=4.0, seed=13).noisy)
        handler, requests = fake_osrm()

        run_match(cleaned, handler)

        assert "timestamps" not in requests[0].url.params

    def test_never_asks_osrm_to_tidy(self, short_trace):
        handler, requests = fake_osrm()

        run_match(short_trace, handler)

        # Tidying drops input points, and the input-index-to-tracepoint mapping
        # the stitching relies on goes with it.
        assert requests[0].url.params["tidy"] == "false"


class TestMatchedGeometry:
    def test_snaps_the_trace_onto_the_road(self, short_trace):
        handler, _ = fake_osrm()

        result = run_match(short_trace, handler)

        # The cleaned trace floats metres off the road; the matched one is on it.
        assert max(ROAD.distance(point) for point in short_trace.geometry) > 1.0
        assert max_offset_from_road(result.geometry) < 1e-6
        assert result.fully_matched
        assert result.matched_fraction == pytest.approx(1.0)

    def test_stitches_a_chunked_trace_without_doubling_back(self, long_trace):
        handler, requests = fake_osrm()

        result = run_match(long_trace, handler)

        # 400 s at 10 m/s. A seam that duplicated its overlap would add ~100 m
        # per chunk here, and one that dropped it would lose as much.
        assert len(requests) >= 4
        assert result.length_m == pytest.approx(4000.0, rel=0.01)
        assert max_offset_from_road(result.geometry) < 1e-6

    def test_the_stitched_line_runs_monotonically_along_the_road(self, long_trace):
        handler, _ = fake_osrm()

        result = run_match(long_trace, handler)

        along = [ROAD.project(Point(c)) for c in result.geometry.coords]
        assert all(np.diff(along) >= -1e-6)

    def test_reports_osrm_confidence(self, short_trace):
        handler, _ = fake_osrm(confidence=0.42)

        result = run_match(short_trace, handler)

        assert result.min_confidence == pytest.approx(0.42)

    def test_result_reprojects_to_plausible_harare_lonlat(self, short_trace):
        handler, _ = fake_osrm()

        lon_min, lat_min, lon_max, lat_max = run_match(short_trace, handler).to_wgs84().bounds

        assert 30.5 < lon_min <= lon_max < 31.5
        assert -18.5 < lat_min <= lat_max < -17.0

    def test_rejects_a_trace_too_short_to_match(self):
        cleaned = clean_trace(synthetic_trace(duration_s=0.0, seed=14).noisy)

        handler, _ = fake_osrm()
        with pytest.raises(ValueError, match="at least two points"):
            run_match(cleaned, handler)


class TestNoMatchFallback:
    def test_falls_back_to_the_cleaned_geometry_for_an_unmatched_chunk(self, long_trace):
        handler, _ = fake_osrm(unmatched_requests=(1,))

        result = run_match(long_trace, handler)

        assert not result.fully_matched
        assert 0.5 < result.matched_fraction < 1.0
        fallbacks = [segment for segment in result.segments if not segment.matched]
        assert len(fallbacks) == 1
        assert "NoMatch" in fallbacks[0].reason

    def test_the_output_is_still_one_connected_line_over_the_whole_trace(self, long_trace):
        handler, _ = fake_osrm(unmatched_requests=(1,))

        result = run_match(long_trace, handler)

        assert isinstance(result.geometry, LineString)
        assert result.geometry.is_valid
        assert result.length_m == pytest.approx(4000.0, rel=0.05)
        assert result.segments[0].start_index == 0
        assert result.segments[-1].end_index == len(long_trace) - 1

    def test_the_fallback_stretch_follows_the_cleaned_points(self, long_trace):
        handler, _ = fake_osrm(unmatched_requests=(1,))

        result = run_match(long_trace, handler)
        fallback = next(segment for segment in result.segments if not segment.matched)

        # Every cleaned point in the fallback's span should lie on it — the
        # anchors at each end are the only coordinates that are not.
        interior = long_trace.iloc[fallback.start_index + 1 : fallback.end_index]
        assert max(fallback.geometry.distance(point) for point in interior.geometry) < 1e-6

    def test_segments_run_in_order_and_meet_end_to_end(self, long_trace):
        handler, _ = fake_osrm(unmatched_requests=(1,))

        result = run_match(long_trace, handler)

        for earlier, later in zip(result.segments, result.segments[1:], strict=False):
            assert earlier.end_index == later.start_index
            join = Point(earlier.geometry.coords[-1]).distance(Point(later.geometry.coords[0]))
            assert join < 1e-6

    def test_logs_the_unmatched_stretch(self, long_trace, caplog):
        handler, _ = fake_osrm(unmatched_requests=(1,))

        with caplog.at_level(logging.WARNING, logger="match"):
            run_match(long_trace, handler)

        assert "NoMatch" in caplog.text

    def test_a_wholly_unmatched_trace_returns_the_cleaned_geometry(self, short_trace, caplog):
        handler, _ = fake_osrm(unmatched_requests=tuple(range(10)))

        with caplog.at_level(logging.WARNING, logger="match"):
            result = run_match(short_trace, handler)

        assert result.matched_fraction == 0.0
        assert len(result.segments) == 1
        assert len(result.geometry.coords) == len(short_trace)
        assert any("matched none of this trace" in r.message for r in caplog.records)

    def test_bridges_a_hole_inside_an_otherwise_matched_response(self, short_trace):
        handler, _ = fake_osrm(null_range=(20, 30))

        result = run_match(short_trace, handler)

        kinds = [segment.matched for segment in result.segments]
        assert kinds == [True, False, True]
        assert result.segments[1].reason == "OSRM matched no road for these points"
        assert result.geometry.is_valid

    def test_a_hole_at_the_start_falls_back_before_the_first_match(self, short_trace):
        handler, _ = fake_osrm(null_range=(0, 8))

        result = run_match(short_trace, handler)

        assert result.segments[0].matched is False
        assert result.segments[0].start_index == 0
        assert result.segments[1].matched is True

    def test_a_hole_at_the_end_falls_back_after_the_last_match(self, short_trace):
        handler, _ = fake_osrm(null_range=(50, 60))

        result = run_match(short_trace, handler)

        assert result.segments[-1].matched is False
        assert result.segments[-1].end_index == len(short_trace) - 1


class TestErrors:
    def test_too_big_is_raised_rather_than_absorbed(self, short_trace):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400, json={"code": "TooBig", "message": "Too many trace coordinates"}
            )

        # A chunking bug must not quietly degrade into an unmatched trace.
        with pytest.raises(OSRMError, match="TooBig"):
            run_match(short_trace, handler)

    def test_a_server_error_is_raised(self, short_trace):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"code": "Error", "message": "boom"})

        with pytest.raises(OSRMError, match="500"):
            run_match(short_trace, handler)

    def test_an_unreachable_osrm_is_raised(self, short_trace):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        with pytest.raises(OSRMError, match="failed"):
            run_match(short_trace, handler)

    def test_a_non_json_body_is_raised(self, short_trace):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(502, text="<html>bad gateway</html>")

        with pytest.raises(OSRMError, match="not JSON"):
            run_match(short_trace, handler)

    def test_rejects_an_overlap_that_is_not_smaller_than_the_chunk(self, short_trace):
        handler, _ = fake_osrm()

        with pytest.raises(ValueError, match="overlap"):
            run_match(short_trace, handler, max_coordinates=50, overlap=50)


class TestRealisticResponseShape:
    def test_handles_a_verbatim_osrm_response(self, short_trace):
        """A response copied from OSRM's docs, not synthesised by the fake."""
        coordinates = to_wgs84([ROAD.interpolate(d) for d in (0.0, 200.0, 400.0)])
        snapped = to_wgs84([ROAD.interpolate(d) for d in np.linspace(0.0, 400.0, len(short_trace))])
        body = {
            "code": "Ok",
            "matchings": [
                {
                    "confidence": 0.83,
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [list(c) for c in coordinates],
                    },
                    "legs": [],
                    "distance": 400.0,
                    "duration": 40.0,
                    "weight_name": "routability",
                    "weight": 40.0,
                }
            ],
            "tracepoints": [
                {
                    "alternatives_count": 0,
                    "waypoint_index": index,
                    "matchings_index": 0,
                    "distance": 3.2,
                    "name": "Samora Machel Ave",
                    "hint": "opaque",
                    "location": list(location),
                }
                for index, location in enumerate(snapped)
            ],
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=json.dumps(body), headers={"content-type": "application/json"}
            )

        result = run_match(short_trace, handler)

        assert result.fully_matched
        assert result.min_confidence == pytest.approx(0.83)
        assert result.length_m == pytest.approx(400.0, rel=0.01)
