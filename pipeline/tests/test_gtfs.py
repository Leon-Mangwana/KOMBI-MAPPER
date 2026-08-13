"""Tests for the GTFS export.

No database: routes and stops are built directly in EPSG:32736 around the same
Harare origin the synthetic traces use, so every field the feed should contain
is known by construction. Departure times are built in **UTC**, as the database
stores them, and asserted against **Africa/Harare** bands, which is the one
conversion in this module that a passing test could not otherwise prove.

What is not covered here is whether MobilityData's validator accepts the output
— that needs Java and a downloaded jar. The assertions below encode the rules
that validator enforces (strictly increasing distances and times, no unnamed
stop, no single-stop trip, no overlapping frequencies), so a regression should
show up here first. See README.md, "Validating the feed".
"""

import io
import zipfile
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from shapely.geometry import LineString, Point

from gtfs import (
    ALL_DAY,
    BANDS,
    DEFAULT_HEADWAY_S,
    FILENAMES,
    MAX_HEADWAY_S,
    MIN_HEADWAY_S,
    Agency,
    EmptyFeedError,
    ExportRoute,
    ExportStop,
    band_for,
    build_feed,
    estimate_headways,
    format_time,
    observed_gaps,
)
from synthetic import DEFAULT_ORIGIN

HARARE = ZoneInfo("Africa/Harare")

AGENCY = Agency(
    agency_id="kombi",
    name="Kombi Route Mapper",
    url="https://kombi.example.org",
    timezone="Africa/Harare",
    lang="en",
)

#: Fixed so that `calendar.txt` is reproducible.
TODAY = date(2026, 8, 12)

#: A straight 4 km corridor east of the origin, sampled every 25 m the way a
#: matched trace is — so simplification has something to do.
CORRIDOR = LineString(
    [(DEFAULT_ORIGIN[0] + offset, DEFAULT_ORIGIN[1]) for offset in range(0, 4001, 25)]
)


def at(x: float, y: float = 0.0) -> Point:
    return Point(DEFAULT_ORIGIN[0] + x, DEFAULT_ORIGIN[1] + y)


def stops_along(*offsets: float, named: bool = False) -> tuple[ExportStop, ...]:
    """Stops at the given distances along `CORRIDOR`, in order."""
    return tuple(
        ExportStop(
            stop_id=index + 1,
            name=f"Stop {chr(ord('A') + index)}" if named else None,
            centroid=at(offset),
        )
        for index, offset in enumerate(offsets)
    )


def utc(day: int, hour: int, minute: int = 0) -> datetime:
    """A UTC departure. Harare is UTC+2, so this is `hour + 2` locally."""
    return datetime(2026, 8, day, hour, minute, tzinfo=UTC)


def a_route(
    route_id: int = 1,
    *,
    stops: tuple[ExportStop, ...] | None = None,
    departures: tuple[datetime, ...] = (),
    speed_mps: float | None = 5.0,
    short_name: str | None = None,
    long_name: str | None = None,
) -> ExportRoute:
    return ExportRoute(
        route_id=route_id,
        short_name=short_name,
        long_name=long_name,
        geom=CORRIDOR,
        stops=stops if stops is not None else stops_along(0.0, 1000.0, 2500.0, 4000.0),
        departures=departures,
        speed_mps=speed_mps,
    )


def feed_for(*routes: ExportRoute, **kwargs):
    return build_feed(list(routes), agency=AGENCY, today=TODAY, **kwargs)


# --------------------------------------------------------------------------
# Time bands and headways
# --------------------------------------------------------------------------


def test_bands_tile_the_operating_day_without_gaps_or_overlap():
    # Overlapping frequencies for one trip is a validator error, and a gap
    # between bands is service silently dropped.
    for earlier, later in zip(BANDS, BANDS[1:], strict=False):
        assert earlier.end_s == later.start_s
    assert ALL_DAY.start_s == BANDS[0].start_s
    assert ALL_DAY.end_s == BANDS[-1].end_s


@pytest.mark.parametrize(
    ("hour", "expected"),
    [(4, "early_morning"), (7, "am_peak"), (12, "midday"), (17, "pm_peak"), (20, "evening")],
)
def test_band_for_picks_the_band_containing_the_time(hour, expected):
    assert band_for(hour * 3600).name == expected


@pytest.mark.parametrize("hour", [2, 23])
def test_band_for_returns_none_outside_the_operating_day(hour):
    assert band_for(hour * 3600) is None


def test_departures_are_banded_in_local_time_not_utc():
    # 05:30 UTC is 07:30 in Harare: the AM peak, not the early morning band.
    _gaps, counts, unbanded = observed_gaps([utc(3, 5, 30)], HARARE)

    assert counts["am_peak"] == 1
    assert counts["early_morning"] == 0
    assert unbanded == 0


def test_gaps_are_not_taken_across_days_or_bands():
    departures = [
        utc(3, 5, 0),  # 07:00 local, am_peak
        utc(3, 5, 20),  # 07:20 local, am_peak  -> one 20-minute gap
        utc(3, 11, 0),  # 13:00 local, midday   -> not a gap from 07:20
        utc(4, 5, 0),  # 07:00 next day        -> not a gap from the day before
    ]

    gaps, counts, _unbanded = observed_gaps(departures, HARARE)

    assert gaps["am_peak"] == [1200.0]
    assert gaps["midday"] == []
    assert counts["am_peak"] == 3
    assert counts["midday"] == 1


def test_departures_outside_the_operating_day_are_counted_nowhere():
    _gaps, counts, unbanded = observed_gaps([utc(3, 0, 30)], HARARE)  # 02:30 local

    assert unbanded == 1
    assert sum(counts.values()) == 0


def test_headway_is_the_median_gap_in_the_band():
    # 07:00, 07:10, 07:25, 07:55 local -> gaps of 10, 15 and 30 minutes.
    departures = [utc(3, 5, 0), utc(3, 5, 10), utc(3, 5, 25), utc(3, 5, 55)]

    headways = {h.band.name: h for h in estimate_headways(departures, HARARE)}

    assert headways["am_peak"].headway_s == 15 * 60
    assert headways["am_peak"].basis == "band"
    assert headways["am_peak"].departures == 4
    assert headways["am_peak"].gaps == 3


def test_only_observed_bands_are_published():
    headways = estimate_headways([utc(3, 5, 0), utc(3, 5, 20), utc(3, 5, 40)], HARARE)

    assert [headway.band.name for headway in headways] == ["am_peak"]


def test_fill_unobserved_bands_publishes_every_band_from_the_route_median():
    departures = [utc(3, 5, 0), utc(3, 5, 20), utc(3, 5, 40)]  # gaps of 20 minutes

    headways = estimate_headways(departures, HARARE, fill_unobserved_bands=True)
    by_name = {headway.band.name: headway for headway in headways}

    assert len(headways) == len(BANDS)
    assert by_name["am_peak"].basis == "band"
    # The unobserved bands borrow the route's own gaps rather than inventing a
    # number, and say so.
    assert by_name["midday"].basis == "route"
    assert by_name["midday"].headway_s == 20 * 60
    assert by_name["midday"].departures == 0


def test_a_band_with_too_few_gaps_falls_back_to_the_route():
    departures = [
        utc(3, 5, 0),  # am_peak 07:00
        utc(3, 5, 12),  # am_peak 07:12  -> one gap only
        utc(3, 5, 24),  # am_peak 07:24
        utc(3, 11, 0),  # midday 13:00   -> no gap at all
    ]

    by_name = {h.band.name: h for h in estimate_headways(departures, HARARE)}

    assert by_name["am_peak"].basis == "band"
    assert by_name["midday"].basis == "route"
    assert by_name["midday"].gaps == 0


def test_a_route_with_no_departures_still_gets_one_all_day_row():
    # A trip with no frequencies row is not a frequency-based trip at all — it
    # becomes a single scheduled departure at the stop_times anchor.
    headways = estimate_headways([], HARARE)

    assert len(headways) == 1
    assert headways[0].band is ALL_DAY
    assert headways[0].basis == "default"
    assert headways[0].headway_s == DEFAULT_HEADWAY_S


def test_headways_are_clamped_and_rounded_to_the_minute():
    minute = timedelta(minutes=1)
    base = utc(3, 5, 0)

    # Gaps of 30 s: below the floor, which exists to reject two contributors on
    # the same kombi.
    close = [base, base + 30 * timedelta(seconds=1), base + 60 * timedelta(seconds=1)]
    assert estimate_headways(close, HARARE)[0].headway_s == MIN_HEADWAY_S

    # Gaps of 90 minutes: above the ceiling, which caps the sparse-coverage
    # bias. Inside midday, which is the one band long enough to hold them.
    midday = utc(3, 7, 0)  # 09:00 local
    sparse = [midday, midday + 90 * minute, midday + 180 * minute]
    assert estimate_headways(sparse, HARARE)[0].headway_s == MAX_HEADWAY_S

    # And an ordinary gap comes back on a whole minute.
    odd = [base, base + timedelta(seconds=743), base + timedelta(seconds=1486)]
    headway = estimate_headways(odd, HARARE)[0].headway_s
    assert headway % 60 == 0
    assert headway == 12 * 60


# --------------------------------------------------------------------------
# Feed structure
# --------------------------------------------------------------------------


def test_feed_contains_every_required_file():
    feed = feed_for(a_route())

    assert set(feed.files) == set(FILENAMES)
    assert all(text.endswith("\n") for text in feed.files.values())


def test_zip_holds_the_files_at_the_archive_root():
    # A feed nested in a folder is the most common way a zip that looks right
    # is rejected.
    with zipfile.ZipFile(io.BytesIO(feed_for(a_route()).to_bytes())) as archive:
        assert archive.namelist() == list(FILENAMES)
        assert archive.read("agency.txt").decode().startswith("agency_id,")


def test_exporting_the_same_network_twice_is_byte_identical():
    route = a_route(departures=(utc(3, 5, 0), utc(3, 5, 20)))

    assert feed_for(route).to_bytes() == feed_for(route).to_bytes()


def test_agency_and_calendar_describe_an_everyday_service():
    feed = feed_for(a_route(departures=(utc(3, 5, 0),)))

    (agency,) = feed.rows("agency.txt")
    assert agency["agency_timezone"] == "Africa/Harare"
    assert agency["agency_url"].startswith("https://")

    (calendar,) = feed.rows("calendar.txt")
    assert all(calendar[day] == "1" for day in ("monday", "saturday", "sunday"))
    # Service starts the day of the earliest observed ride and runs from today,
    # so a feed built from old traces is not born expired.
    assert calendar["start_date"] == "20260803"
    assert calendar["end_date"] == "20270812"


def test_stops_are_named_and_positioned_in_wgs84():
    feed = feed_for(a_route(stops=stops_along(0.0, 2000.0, named=True)))

    first, second = feed.rows("stops.txt")
    assert first["stop_name"] == "Stop A"
    # Harare, and the second stop 2 km east of the first. Not due east: a line
    # of constant UTM northing is not a parallel, so the latitude moves a little.
    assert -18.0 < float(first["stop_lat"]) < -17.0
    assert 30.0 < float(first["stop_lon"]) < 32.0
    assert float(second["stop_lon"]) > float(first["stop_lon"])
    assert float(second["stop_lat"]) == pytest.approx(float(first["stop_lat"]), abs=1e-3)


def test_an_unnamed_stop_still_gets_a_stop_name():
    # GTFS requires stop_name; clustering cannot produce one.
    feed = feed_for(a_route(stops=stops_along(0.0, 2000.0)))

    assert [row["stop_name"] for row in feed.rows("stops.txt")] == ["Stop 1", "Stop 2"]


def test_a_stop_is_listed_once_however_many_routes_serve_it():
    shared = stops_along(0.0, 1000.0, 2000.0)
    feed = feed_for(a_route(1, stops=shared), a_route(2, stops=shared))

    assert len(feed.rows("stops.txt")) == 3
    assert len(feed.rows("trips.txt")) == 2


def test_route_names_fall_back_to_the_terminals_and_then_to_the_id():
    named = feed_for(a_route(stops=stops_along(0.0, 2000.0, named=True)))
    (route,) = named.rows("routes.txt")
    assert route["route_long_name"] == "Stop A – Stop B"
    assert route["route_short_name"] == ""

    unnamed = feed_for(a_route(stops=stops_along(0.0, 2000.0)))
    (route,) = unnamed.rows("routes.txt")
    assert route["route_short_name"] == "R1"
    assert route["route_long_name"] == ""


def test_a_human_set_name_wins():
    feed = feed_for(a_route(short_name="14", long_name="Mbare – Msasa"))

    (route,) = feed.rows("routes.txt")
    assert route["route_short_name"] == "14"
    assert route["route_long_name"] == "Mbare – Msasa"
    assert route["route_type"] == "3"


def test_a_route_with_fewer_than_two_stops_is_excluded():
    # One stop is not a trip anyone can take, and the validator rejects it.
    feed = feed_for(a_route(1), a_route(2, stops=stops_along(0.0)))

    assert [row["route_id"] for row in feed.rows("routes.txt")] == ["1"]
    assert [row["trip_id"] for row in feed.rows("trips.txt")] == ["trip_1"]


def test_a_network_with_nothing_exportable_raises_rather_than_writing_an_empty_feed():
    with pytest.raises(EmptyFeedError, match="at least two stops"):
        feed_for(a_route(1, stops=stops_along(0.0)))


# --------------------------------------------------------------------------
# The reference trip
# --------------------------------------------------------------------------


def test_stop_times_are_offsets_from_a_midnight_anchor():
    # 4 km at 5 m/s: the last stop is 800 s after the first.
    feed = feed_for(a_route(speed_mps=5.0))

    rows = feed.rows("stop_times.txt")
    assert [row["stop_sequence"] for row in rows] == ["1", "2", "3", "4"]
    assert rows[0]["departure_time"] == "00:00:00"
    assert rows[-1]["arrival_time"] == format_time(4000 // 5)
    assert all(row["timepoint"] == "0" for row in rows)
    assert all(row["trip_id"] == "trip_1" for row in rows)


def test_a_faster_route_has_a_shorter_reference_trip():
    slow = feed_for(a_route(speed_mps=4.0)).rows("stop_times.txt")[-1]["arrival_time"]
    fast = feed_for(a_route(speed_mps=8.0)).rows("stop_times.txt")[-1]["arrival_time"]

    assert fast < slow


def test_a_route_with_no_timed_traces_uses_the_default_speed():
    feed = feed_for(a_route(speed_mps=None), default_speed_mps=5.0)

    assert feed.rows("stop_times.txt")[-1]["arrival_time"] == format_time(800)


def test_stop_times_and_distances_are_strictly_increasing():
    # Two stops 3 m apart round to the same second and the same metre; the
    # validator rejects both, so they are forced apart.
    feed = feed_for(a_route(stops=stops_along(0.0, 1000.0, 1003.0, 4000.0)))

    rows = feed.rows("stop_times.txt")
    times = [row["arrival_time"] for row in rows]
    distances = [float(row["shape_dist_traveled"]) for row in rows]

    assert times == sorted(times)
    assert len(set(times)) == len(times)
    assert all(later > earlier for earlier, later in zip(distances, distances[1:], strict=False))


def test_stop_distances_lie_on_the_shape():
    feed = feed_for(a_route())

    shape = [float(row["shape_dist_traveled"]) for row in feed.rows("shapes.txt")]
    stop_distances = [float(row["shape_dist_traveled"]) for row in feed.rows("stop_times.txt")]

    assert shape[0] == 0.0
    assert max(stop_distances) <= max(shape)
    # The corridor is 4 km long and the stops span it end to end.
    assert max(shape) == pytest.approx(4000.0, abs=1.0)
    assert stop_distances[-1] == pytest.approx(4000.0, abs=1.0)


def test_shape_points_are_sequential_and_strictly_advance():
    feed = feed_for(a_route())

    rows = feed.rows("shapes.txt")
    assert [row["shape_pt_sequence"] for row in rows] == [
        str(index) for index in range(1, len(rows) + 1)
    ]
    assert all(row["shape_id"] == "shape_1" for row in rows)

    distances = [float(row["shape_dist_traveled"]) for row in rows]
    assert all(later > earlier for earlier, later in zip(distances, distances[1:], strict=False))


def test_the_shape_is_simplified_but_keeps_its_ends():
    # 161 vertices at 25 m spacing down a straight line collapse to the two ends.
    feed = feed_for(a_route(), shape_tolerance_m=5.0)

    rows = feed.rows("shapes.txt")
    assert len(rows) < len(CORRIDOR.coords)
    assert float(rows[0]["shape_dist_traveled"]) == 0.0
    assert float(rows[-1]["shape_dist_traveled"]) == pytest.approx(4000.0, abs=1.0)


def test_the_trip_is_headsigned_with_its_last_stop():
    feed = feed_for(a_route(stops=stops_along(0.0, 2000.0, named=True)))

    (trip,) = feed.rows("trips.txt")
    assert trip["trip_headsign"] == "Stop B"
    assert trip["shape_id"] == "shape_1"
    assert trip["service_id"] == "ALL"


# --------------------------------------------------------------------------
# frequencies.txt
# --------------------------------------------------------------------------


def test_frequencies_are_inexact_and_cover_only_observed_bands():
    departures = (utc(3, 5, 0), utc(3, 5, 15), utc(3, 5, 30))  # 07:00-07:30 local
    feed = feed_for(a_route(departures=departures))

    rows = feed.rows("frequencies.txt")
    assert len(rows) == 1
    assert rows[0]["trip_id"] == "trip_1"
    assert rows[0]["start_time"] == "06:00:00"
    assert rows[0]["end_time"] == "09:00:00"
    assert rows[0]["headway_secs"] == str(15 * 60)
    # exact_times=0 is the whole point: a kombi leaves when it is full.
    assert rows[0]["exact_times"] == "0"


def test_every_trip_has_at_least_one_frequency_row():
    feed = feed_for(a_route(1), a_route(2, departures=(utc(3, 5, 0),)))

    trips = {row["trip_id"] for row in feed.rows("trips.txt")}
    assert {row["trip_id"] for row in feed.rows("frequencies.txt")} == trips


def test_a_trips_frequency_rows_never_overlap():
    departures = tuple(utc(3, hour, minute) for hour in (5, 11, 16) for minute in (0, 20, 40))
    feed = feed_for(a_route(departures=departures))

    rows = feed.rows("frequencies.txt")
    assert len(rows) == 3
    for earlier, later in zip(rows, rows[1:], strict=False):
        assert earlier["end_time"] <= later["start_time"]
