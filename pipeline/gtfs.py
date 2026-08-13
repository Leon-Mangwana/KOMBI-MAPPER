"""GTFS export: the clustered network out as a zipped feed.

This is the last stage. `cluster.py` has already decided what the routes and
stops are; everything here is a translation of those rows into the eight files
GTFS wants, plus the one genuinely hard question — *when does a kombi run?*

**Kombis have no timetable.** A kombi leaves the rank when it is full. Nobody
publishes a departure time, no departure time is adhered to, and no amount of
trace collection will produce one, because there is nothing there to observe.
GTFS has a facility for exactly this case and it is `frequencies.txt`: a single
reference trip in `stop_times.txt` describing the *pattern* — which stops, in
what order, how long apart — and then, per time band, a headway saying how often
that pattern repeats. `exact_times=0` marks it as "roughly every N minutes, no
guaranteed clock times", which is what a kombi service actually is.

The alternative — synthesising one `trips.txt` row per notional departure at
06:00, 06:15, 06:30 and so on — would produce a feed of exactly the same shape
that a trip planner would render as a timetable, promising a passenger a
departure at 06:15 that no operator has ever committed to. Every number in it
would be invented here rather than measured. So: frequencies, and the headways
come from the intervals between the departures we have actually seen.

**Where headways come from.** Every clustered trace is one observed departure,
timestamped at its first point. Departures are bucketed into five bands of the
local operating day (`BANDS`), grouped by calendar date, and the gaps between
consecutive departures *within* one band on one day are pooled across days; the
band's headway is the median of those gaps.

The bias in that is worth stating plainly, because it is large and it is
one-sided: contributors ride some kombis and not others, so an observed gap
spans every departure nobody recorded. The estimate is therefore an **upper
bound** on the true headway, and it tightens as coverage improves. It is not a
guess, though — it is a measurement of a real quantity that converges on the
right answer, which is more than a fabricated timetable can say.

**Where the running times come from.** `stop_times.txt` still needs times, and
they are treated as what the spec says they are for a frequency-based trip:
offsets from the trip's own start, not clock times. The reference trip is
anchored at 00:00:00 and each stop's offset is its distance along the route
divided by the route's observed operating speed — the median over the route's
traces of matched length over wall-clock duration, which already has stop dwells
and traffic baked into it. `timepoint=0` on every row marks the times as
approximate, which they are.

Metric work is in EPSG:32736 and geometry is reprojected to EPSG:4326 on the way
out, as everywhere else. See ../CLAUDE.md.

Validating the output is documented in README.md ("Validating the feed").
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import zipfile
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from math import hypot
from pathlib import Path
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import geopandas as gpd
import pandas as pd
import shapely
from shapely.geometry import LineString, Point
from sqlalchemy import Engine, text

from clean import UTM36S, WGS84
from config import get_engine, settings

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

#: `route_type` 3, "Bus". A kombi is a 15-seat minibus in mixed traffic, which
#: is a bus for every purpose a trip planner has. The extended route types have
#: a finer code (704, "local bus"), but consumer support for them is patchy and
#: nothing downstream would do anything different with it.
ROUTE_TYPE_BUS = 3

#: The one service in `calendar.txt`. Kombis run every day; there is no weekday
#: or school-holiday variant to model, so one always-on service covers the feed.
SERVICE_ID = "ALL"

#: How long the exported `calendar.txt` claims service for. A feed whose
#: `end_date` has passed is treated as expired by every consumer, and the
#: validator raises it as an error, so this has to comfortably outlast the gap
#: between exports.
DEFAULT_SERVICE_DAYS = 365

#: Headway used when there is nothing to measure — a route whose traces carry no
#: timestamps, or a band being filled in by `fill_unobserved_bands`. 15 minutes
#: is the order of magnitude a busy Harare route runs at; it is a placeholder,
#: and `Headway.basis` says so on every row that uses it.
DEFAULT_HEADWAY_S = 900

#: Bounds on a published headway. The floor rejects two contributors boarding
#: the same kombi (or the one behind it) and reading as a 30-second service; the
#: ceiling caps the sparse-coverage bias, where one recorded morning ride and one
#: recorded afternoon ride would otherwise imply a six-hour headway. Both are
#: about the *estimator*, not about kombis — hitting either is a sign the band
#: has too little data, and both are logged.
MIN_HEADWAY_S = 120
MAX_HEADWAY_S = 3600

#: Gaps a band needs before its own median is used rather than the route's. Two
#: gaps means three recorded departures in one band on one day — enough that the
#: median is a median rather than a single observation.
MIN_GAPS_FOR_HEADWAY = 2

#: Fallback operating speed, in m/s (18 km/h), for a route with no usable
#: timestamps. Deliberately an *average including stops*, not a cruising speed.
DEFAULT_SPEED_MPS = 5.0

#: Bounds on the observed speed, in m/s: 7.2 km/h to 54 km/h. Outside these the
#: trace's duration or its matched length is wrong — a recording left running
#: after the ride ended drags the average towards walking pace.
MIN_SPEED_MPS = 2.0
MAX_SPEED_MPS = 15.0

#: Douglas–Peucker tolerance for `shapes.txt`. The canonical geometry is a full
#: 1 Hz matched trace: a 40-minute ride is ~2400 vertices, and at 5 m the line
#: that survives is indistinguishable at any zoom a trip planner draws while
#: being several times smaller. Stop distances are measured on the *simplified*
#: line so `shape_dist_traveled` stays consistent between the two files.
SHAPE_TOLERANCE_M = 5.0

#: Smallest difference two `shape_dist_traveled` values can have and still be
#: distinct once written at two decimal places. The validator rejects a
#: non-increasing distance, and two vertices 3 mm apart print as equal.
DIST_EPSILON_M = 0.01

#: Feed file order. Matches the order the files are described in the GTFS
#: reference, and fixes the order of entries in the zip.
FILENAMES = (
    "agency.txt",
    "stops.txt",
    "routes.txt",
    "trips.txt",
    "stop_times.txt",
    "shapes.txt",
    "calendar.txt",
    "frequencies.txt",
)


class EmptyFeedError(RuntimeError):
    """Raised when there is nothing exportable — see `build_feed`."""


@dataclass(frozen=True)
class TimeBand:
    """A stretch of the local operating day with its own headway.

    Bounds are seconds since local midnight, half-open: `start_s` is in the
    band, `end_s` is the start of the next one.
    """

    name: str
    start_s: int
    end_s: int

    def contains(self, seconds: int) -> bool:
        return self.start_s <= seconds < self.end_s

    @property
    def duration_s(self) -> int:
        return self.end_s - self.start_s


def _hours(value: float) -> int:
    return int(value * 3600)


#: The operating day, split where Harare's kombi demand actually changes.
#:
#: The peaks are the commute — kombis run nose to tail into town from about six
#: and back out from about three, and the two shoulders either side are a
#: different service entirely. Midday is one long band rather than several
#: because nothing distinguishes 10:00 from 14:00 on a kombi route: the rank
#: fills slowly and the headway is flat across the whole of it.
#:
#: The day starts at 04:00 and ends at 22:00. Outside that there is essentially
#: no service, and a departure recorded at 02:00 is far more likely to be a
#: clock problem or a contributor who forgot to stop recording than a kombi;
#: those observations are counted in no band, and the count is logged.
BANDS: tuple[TimeBand, ...] = (
    TimeBand("early_morning", _hours(4), _hours(6)),
    TimeBand("am_peak", _hours(6), _hours(9)),
    TimeBand("midday", _hours(9), _hours(15)),
    TimeBand("pm_peak", _hours(15), _hours(19)),
    TimeBand("evening", _hours(19), _hours(22)),
)

#: The band a route falls back to when it has no usable departure at all. It
#: spans the whole operating day, so a trip planner still finds the route.
ALL_DAY = TimeBand("all_day", BANDS[0].start_s, BANDS[-1].end_s)


# --------------------------------------------------------------------------
# The data an export needs
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Agency:
    """The single agency the feed is published under.

    Informal operators have no agency in the GTFS sense — there is no company
    that runs the Mbare–Msasa kombi, there are a hundred owner-drivers — but
    `agency.txt` is required and every route must belong to one. So the feed
    publisher is the agency, which is the honest reading of who is making this
    claim about the network.
    """

    agency_id: str
    name: str
    url: str
    timezone: str
    lang: str | None = "en"

    @classmethod
    def from_settings(cls) -> Agency:
        return cls(
            agency_id=settings.gtfs_agency_id,
            name=settings.gtfs_agency_name,
            url=settings.gtfs_agency_url,
            timezone=settings.gtfs_agency_timezone,
            lang=settings.gtfs_agency_lang or None,
        )

    @property
    def zone(self) -> ZoneInfo:
        """The agency timezone, which is what the time bands are read in.

        Trace timestamps are UTC; `BANDS` are local clock times, and a kombi's
        morning peak is a local phenomenon. Harare is UTC+2 with no DST, so the
        conversion is a fixed offset — but it is done properly through the tz
        database anyway, because a hard-coded +2 is exactly the sort of thing
        that survives until the feed covers a second city.
        """
        try:
            return ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as error:  # pragma: no cover - env-specific
            raise ValueError(
                f"Unknown agency timezone {self.timezone!r}. "
                "It must be an IANA name such as 'Africa/Harare'; "
                "on a system with no tz database, `pip install tzdata`."
            ) from error


@dataclass(frozen=True)
class ExportStop:
    """A stop as the exporter needs it: identity, name and position.

    `centroid` is in EPSG:32736, as `cluster.StopCluster.centroid` was — the
    reprojection to EPSG:4326 happens once, at write time.
    """

    stop_id: int
    name: str | None
    centroid: Point

    def display_name(self) -> str:
        """A non-empty `stop_name`, which GTFS requires and the database does not.

        `stops.name` is the column clustering cannot fill in — a stop is a
        cluster of dwells, and only a person can say that it is the Copacabana
        rank. Until somebody does, the id is at least unambiguous and stable.
        """
        name = (self.name or "").strip()
        return name if name else f"Stop {self.stop_id}"


@dataclass(frozen=True)
class ExportRoute:
    """One canonical route, with everything the feed needs about it.

    Attributes:
        route_id: `routes.id`, used verbatim as the GTFS `route_id`.
        short_name: `routes.short_name`, if a human has set one.
        long_name: `routes.long_name`, if a human has set one.
        geom: The canonical geometry in EPSG:32736.
        stops: The route's stops in order along `geom` — `route_stops.seq`.
        departures: One timezone-aware timestamp per observed ride, being the
            time its trace starts. Ascending.
        speed_mps: Median observed operating speed over the route's traces,
            including time spent stopped. `None` when no trace had usable
            timestamps.
    """

    route_id: int
    short_name: str | None
    long_name: str | None
    geom: LineString
    stops: tuple[ExportStop, ...]
    departures: tuple[datetime, ...] = ()
    speed_mps: float | None = None

    @property
    def trip_id(self) -> str:
        return f"trip_{self.route_id}"

    @property
    def shape_id(self) -> str:
        return f"shape_{self.route_id}"

    def names(self) -> tuple[str, str]:
        """`(route_short_name, route_long_name)`, at least one non-empty.

        GTFS requires one of the two. Where a human has named neither, the
        terminals make a serviceable long name — "Mbare – Msasa" is how the
        route is described at the rank anyway — and failing that the id stands
        in, so the route is still identifiable in a trip planner.
        """
        short = (self.short_name or "").strip()
        long = (self.long_name or "").strip()

        if not long and len(self.stops) >= 2:
            first, last = self.stops[0], self.stops[-1]
            if (first.name or "").strip() and (last.name or "").strip():
                long = f"{first.name.strip()} – {last.name.strip()}"

        if not short and not long:
            short = f"R{self.route_id}"

        return short, long


@dataclass(frozen=True)
class Headway:
    """One `frequencies.txt` row before it is written out.

    Attributes:
        band: The time band it covers.
        headway_s: Seconds between departures, rounded to the minute.
        basis: Where the number came from — `"band"` for this band's own
            observed gaps, `"route"` for the route's gaps pooled across bands,
            `"default"` for `DEFAULT_HEADWAY_S` because there was nothing to
            measure. Not written to the feed, which has nowhere to put it;
            reported by the CLI, where somebody reviewing the export can see how
            much of it is measured.
        departures: Observed departures in this band.
        gaps: Observed intervals in this band, which is `departures` minus one
            per day that had any.
    """

    band: TimeBand
    headway_s: int
    basis: str
    departures: int
    gaps: int

    @property
    def measured(self) -> bool:
        return self.basis == "band"


@dataclass(frozen=True)
class Feed:
    """A built feed: file name to CSV text, in `FILENAMES` order."""

    files: dict[str, str]

    def to_bytes(self) -> bytes:
        """The feed as a zip archive.

        Files sit at the archive root — a feed nested inside a folder is the
        single most common way a zip that looks right is rejected.

        Timestamps inside the archive are fixed rather than "now", so exporting
        the same network twice produces byte-identical output. That makes "has
        the network changed?" a question `sha256sum` can answer, which is
        useful when the feed is served from a static file and consumers cache
        on ETag.
        """
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in FILENAMES:
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                archive.writestr(info, self.files[name])
        return buffer.getvalue()

    def write(self, path: str | Path) -> Path:
        """Write the zip to `path`, creating parent directories."""
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.to_bytes())
        return destination

    def rows(self, filename: str) -> list[dict[str, str]]:
        """Parse one file back into dicts. For tests and for eyeballing output."""
        return list(csv.DictReader(io.StringIO(self.files[filename])))


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


_ROUTES_SQL = text(
    """
    SELECT id, short_name, long_name, ST_AsText(geom) AS wkt
    FROM routes
    WHERE geom IS NOT NULL
      AND ST_NumPoints(geom) >= 2
    ORDER BY id
    """
)

_ROUTE_STOPS_SQL = text(
    """
    SELECT rs.route_id, rs.seq, s.id AS stop_id, s.name, ST_AsText(s.geom) AS wkt
    FROM route_stops rs
    JOIN stops s ON s.id = rs.stop_id
    ORDER BY rs.route_id, rs.seq
    """
)

#: One row per clustered trace: when it started, how far it ran, how long it
#: took. `traces.recorded_at` is set at ingest to the earliest point timestamp,
#: which is the departure; the aggregate is a fallback for rows written before
#: that was true. Length comes from the *matched* geometry rather than the
#: route's canonical one so that a contributor who rode half the route
#: contributes a speed rather than a wrong speed.
_TRACE_RUNS_SQL = text(
    """
    SELECT t.route_id,
           t.id AS trace_id,
           COALESCE(t.recorded_at, MIN(p.recorded_at)) AS departure,
           ST_Length(ST_Transform(t.matched_geom, 32736)) AS length_m,
           EXTRACT(EPOCH FROM (MAX(p.recorded_at) - MIN(p.recorded_at))) AS duration_s
    FROM traces t
    LEFT JOIN trace_points p ON p.trace_id = t.id
    WHERE t.route_id IS NOT NULL
    GROUP BY t.id
    ORDER BY t.route_id, departure
    """
)


def load_export_network(engine: Engine | None = None) -> list[ExportRoute]:
    """Read the persisted network and everything the feed needs about it.

    Reads `routes`, `route_stops`, `stops` and `traces` — the tables
    `cluster.persist_network` wrote. Geometry comes back reprojected to
    EPSG:32736.

    Routes with fewer than two stops are returned as they are; `build_feed`
    is where they are dropped, so that the reason is in one place.

    Returns:
        One `ExportRoute` per route, ordered by id. Empty if clustering has not
        run.
    """
    engine = engine if engine is not None else get_engine()

    with engine.connect() as conn:
        route_rows = conn.execute(_ROUTES_SQL).mappings().all()
        stop_rows = conn.execute(_ROUTE_STOPS_SQL).mappings().all()
        run_rows = conn.execute(_TRACE_RUNS_SQL).mappings().all()

    if not route_rows:
        logger.warning("No routes found; run cluster.py before exporting GTFS.")
        return []

    geometries = _to_utm([row["wkt"] for row in route_rows])
    stop_points = _to_utm([row["wkt"] for row in stop_rows])

    stops_by_route: dict[int, list[ExportStop]] = defaultdict(list)
    for row, point in zip(stop_rows, stop_points, strict=True):
        stops_by_route[int(row["route_id"])].append(
            ExportStop(stop_id=int(row["stop_id"]), name=row["name"], centroid=point)
        )

    departures_by_route: dict[int, list[datetime]] = defaultdict(list)
    speeds_by_route: dict[int, list[float]] = defaultdict(list)
    for row in run_rows:
        route_id = int(row["route_id"])
        if row["departure"] is not None:
            departures_by_route[route_id].append(_as_utc(row["departure"]))

        speed = _trace_speed(row["length_m"], row["duration_s"])
        if speed is not None:
            speeds_by_route[route_id].append(speed)

    routes: list[ExportRoute] = []
    for row, geometry in zip(route_rows, geometries, strict=True):
        route_id = int(row["id"])
        speeds = speeds_by_route.get(route_id, [])
        routes.append(
            ExportRoute(
                route_id=route_id,
                short_name=row["short_name"],
                long_name=row["long_name"],
                geom=geometry,
                stops=tuple(stops_by_route.get(route_id, ())),
                departures=tuple(sorted(departures_by_route.get(route_id, ()))),
                speed_mps=median(speeds) if speeds else None,
            )
        )

    logger.info(
        "Loaded %d route(s), %d route-stop link(s) and %d observed departure(s).",
        len(routes),
        len(stop_rows),
        sum(len(route.departures) for route in routes),
    )
    return routes


def _trace_speed(length_m: float | None, duration_s: float | None) -> float | None:
    """Average speed over one ride, or `None` if the ride cannot say.

    Wall-clock over matched length, so time spent standing at stops is included
    — which is what the reference trip needs, since it has no separate dwell
    allowance. Values outside `MIN_SPEED_MPS`..`MAX_SPEED_MPS` are dropped
    rather than clamped: a trace that implies walking pace is not a slow kombi,
    it is a recording that kept running after the ride, and the median of the
    route's other traces is a better answer than a clamped wrong one.
    """
    if length_m is None or duration_s is None or duration_s <= 0 or length_m <= 0:
        return None

    speed = float(length_m) / float(duration_s)
    if not MIN_SPEED_MPS <= speed <= MAX_SPEED_MPS:
        logger.debug("Discarding implausible trace speed of %.1f m/s.", speed)
        return None
    return speed


def _as_utc(moment: datetime | pd.Timestamp) -> datetime:
    """Coerce a database timestamp to an aware UTC `datetime`.

    `traces.recorded_at` is `timestamptz` so the driver returns it aware, but a
    naive value would silently be read as local time in `estimate_headways` and
    land in the wrong band. Naive is assumed UTC, which is how it was stored.
    """
    if isinstance(moment, pd.Timestamp):
        moment = moment.to_pydatetime()
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _to_utm(wkts: Sequence[str]) -> list[LineString | Point]:
    """Parse WKT stored as EPSG:4326 and reproject it to EPSG:32736."""
    if not wkts:
        return []
    series = gpd.GeoSeries(shapely.from_wkt(list(wkts)), crs=WGS84).to_crs(UTM36S)
    return list(series)


def _to_wgs84(geometry: LineString | Point) -> LineString | Point:
    """Reproject one EPSG:32736 geometry back to EPSG:4326, as GTFS wants it."""
    return gpd.GeoSeries([geometry], crs=UTM36S).to_crs(WGS84).iloc[0]


# --------------------------------------------------------------------------
# Headways
# --------------------------------------------------------------------------


def band_for(seconds: int, bands: Sequence[TimeBand] = BANDS) -> TimeBand | None:
    """The band a local time-of-day falls in, or `None` outside the day."""
    for band in bands:
        if band.contains(seconds):
            return band
    return None


def _seconds_of_day(moment: datetime) -> int:
    return moment.hour * 3600 + moment.minute * 60 + moment.second


def observed_gaps(
    departures: Iterable[datetime],
    zone: ZoneInfo,
    bands: Sequence[TimeBand] = BANDS,
) -> tuple[dict[str, list[float]], dict[str, int], int]:
    """Intervals between consecutive observed departures, per band.

    Departures are converted to agency-local time, bucketed by band and by local
    calendar date, and differenced within each (band, date) group. Both groupings
    matter:

    - **By date**, because the interval from the last kombi on Tuesday to the
      first on Wednesday is an overnight closure, not a headway.
    - **By band**, because the interval from an 08:00 departure to a 14:00 one
      spans a peak and a midday lull and describes neither.

    Returns:
        `(gaps, counts, unbanded)` — seconds between consecutive departures
        keyed by band name, the number of departures seen in each band, and the
        number of departures that fell outside every band.
    """
    by_band_day: dict[str, dict[date, list[int]]] = {band.name: defaultdict(list) for band in bands}
    counts = {band.name: 0 for band in bands}
    unbanded = 0

    for moment in departures:
        local = moment.astimezone(zone)
        band = band_for(_seconds_of_day(local), bands)
        if band is None:
            unbanded += 1
            continue
        by_band_day[band.name][local.date()].append(_seconds_of_day(local))
        counts[band.name] += 1

    gaps: dict[str, list[float]] = {}
    for band in bands:
        band_gaps: list[float] = []
        for times in by_band_day[band.name].values():
            times.sort()
            band_gaps.extend(float(b - a) for a, b in zip(times, times[1:], strict=False))
        gaps[band.name] = band_gaps

    return gaps, counts, unbanded


def estimate_headways(
    departures: Iterable[datetime],
    zone: ZoneInfo,
    *,
    bands: Sequence[TimeBand] = BANDS,
    default_headway_s: int = DEFAULT_HEADWAY_S,
    min_gaps: int = MIN_GAPS_FOR_HEADWAY,
    fill_unobserved_bands: bool = False,
) -> list[Headway]:
    """Turn observed departures into one headway per band.

    A band is published when a departure has been recorded in it — that is the
    evidence the route runs at that hour, and inventing service for the bands
    nobody has ever ridden is the failure mode this whole module exists to
    avoid. `fill_unobserved_bands` overrides that for a feed that has to look
    complete; the filled bands carry `basis != "band"`, so they can still be
    told apart here even though GTFS has nowhere to record it.

    Each band's headway is the median of its own gaps where it has enough of
    them, the median of the route's gaps pooled across bands where it does not,
    and `default_headway_s` where the route has no gaps at all. Medians rather
    than means throughout: one contributor recording two rides ten minutes apart
    and then nothing for four hours should not move the estimate to two hours.

    Always returns at least one row. A route with no usable departure gets a
    single `ALL_DAY` band at the default headway, because a route that appears
    in `trips.txt` with no `frequencies.txt` row is not a frequency-based trip
    at all — it silently becomes one scheduled departure at midnight.

    Returns:
        One `Headway` per published band, in band order.
    """
    departures = list(departures)
    gaps, counts, unbanded = observed_gaps(departures, zone, bands)
    if unbanded:
        logger.info(
            "%d departure(s) fell outside %02d:00-%02d:00 and were not counted.",
            unbanded,
            bands[0].start_s // 3600,
            bands[-1].end_s // 3600,
        )

    pooled = [gap for band_gaps in gaps.values() for gap in band_gaps]

    headways: list[Headway] = []
    for band in bands:
        band_gaps = gaps[band.name]
        observed = counts[band.name]
        if not observed and not fill_unobserved_bands:
            continue

        if len(band_gaps) >= min_gaps:
            seconds, basis = median(band_gaps), "band"
        elif len(pooled) >= min_gaps:
            seconds, basis = median(pooled), "route"
        else:
            seconds, basis = float(default_headway_s), "default"

        headways.append(
            Headway(
                band=band,
                headway_s=_round_headway(seconds),
                basis=basis,
                departures=observed,
                gaps=len(band_gaps),
            )
        )

    if not headways:
        headways.append(
            Headway(
                band=ALL_DAY,
                headway_s=_round_headway(float(default_headway_s)),
                basis="default",
                departures=0,
                gaps=0,
            )
        )

    return headways


def _round_headway(seconds: float) -> int:
    """Clamp to the publishable range and round to the nearest minute.

    Rounding is not cosmetic. A median of 743 seconds asserts a precision the
    estimate does not have — the underlying observation is "about a dozen
    minutes" — and rounding says so.
    """
    clamped = min(max(seconds, MIN_HEADWAY_S), MAX_HEADWAY_S)
    if clamped != seconds:
        logger.debug("Clamped a headway of %.0f s to %.0f s.", seconds, clamped)
    return max(MIN_HEADWAY_S, int(round(clamped / 60.0)) * 60)


# --------------------------------------------------------------------------
# Building the feed
# --------------------------------------------------------------------------


def build_feed(
    routes: Sequence[ExportRoute],
    *,
    agency: Agency | None = None,
    bands: Sequence[TimeBand] = BANDS,
    default_headway_s: int = DEFAULT_HEADWAY_S,
    fill_unobserved_bands: bool = False,
    shape_tolerance_m: float = SHAPE_TOLERANCE_M,
    default_speed_mps: float = DEFAULT_SPEED_MPS,
    service_days: int = DEFAULT_SERVICE_DAYS,
    today: date | None = None,
) -> Feed:
    """Build every feed file from loaded routes. No database, no I/O.

    Routes with fewer than two stops are skipped: a one-stop trip is not a trip
    a passenger can take, and the validator rejects it outright. That is a
    clustering result worth seeing, so each one is logged.

    Args:
        routes: From `load_export_network`, or built directly in tests.
        agency: Feed publisher. Defaults to the configured one.
        bands: Time bands for `frequencies.txt`.
        default_headway_s: Headway where nothing was observed.
        fill_unobserved_bands: Publish every band rather than only the observed
            ones. See `estimate_headways`.
        shape_tolerance_m: Douglas–Peucker tolerance for `shapes.txt`.
        default_speed_mps: Operating speed for a route with no timed traces.
        service_days: Length of the service window in `calendar.txt`.
        today: The current date, for the service window. Injectable so that the
            output is reproducible in tests.

    Returns:
        The `Feed`.

    Raises:
        EmptyFeedError: No route has two stops, so there is nothing to export.
            Raised rather than writing an empty-but-well-formed feed, which is
            invalid GTFS and reads as "the network is gone" to a consumer that
            already has a copy.
    """
    agency = agency if agency is not None else Agency.from_settings()
    zone = agency.zone
    today = today if today is not None else datetime.now(zone).date()

    exportable = [route for route in routes if len(route.stops) >= 2]
    for route in routes:
        if len(route.stops) < 2:
            logger.warning(
                "Route %d has %d stop(s) and cannot be a trip; excluding it from the feed.",
                route.route_id,
                len(route.stops),
            )
    if not exportable:
        raise EmptyFeedError(
            "No route has at least two stops, so the feed would have no trips. "
            "Run cluster.py first, and check that stops survived STOP_MIN_SUPPORT."
        )

    stops: dict[int, ExportStop] = {}
    route_rows: list[Sequence[object]] = []
    trip_rows: list[Sequence[object]] = []
    stop_time_rows: list[Sequence[object]] = []
    shape_rows: list[Sequence[object]] = []
    frequency_rows: list[Sequence[object]] = []

    for route in exportable:
        short_name, long_name = route.names()
        route_rows.append([route.route_id, agency.agency_id, short_name, long_name, ROUTE_TYPE_BUS])
        trip_rows.append(
            [
                route.route_id,
                SERVICE_ID,
                route.trip_id,
                route.stops[-1].display_name(),
                route.shape_id,
            ]
        )

        shape = _shape_points(route.geom, shape_tolerance_m)
        shape_rows.extend(
            [route.shape_id, f"{lat:.6f}", f"{lon:.6f}", sequence, f"{distance:.2f}"]
            for sequence, (lon, lat, distance) in enumerate(shape, start=1)
        )

        speed = route.speed_mps if route.speed_mps is not None else default_speed_mps
        stop_time_rows.extend(_stop_time_rows(route, shape_tolerance_m, speed))
        for stop in route.stops:
            stops[stop.stop_id] = stop

        for headway in estimate_headways(
            route.departures,
            zone,
            bands=bands,
            default_headway_s=default_headway_s,
            fill_unobserved_bands=fill_unobserved_bands,
        ):
            frequency_rows.append(
                [
                    route.trip_id,
                    format_time(headway.band.start_s),
                    format_time(headway.band.end_s),
                    headway.headway_s,
                    0,  # exact_times: see the module docstring.
                ]
            )

    start_date, end_date = _service_window(exportable, zone, today, service_days)

    files = {
        "agency.txt": _csv(
            ("agency_id", "agency_name", "agency_url", "agency_timezone", "agency_lang"),
            [[agency.agency_id, agency.name, agency.url, agency.timezone, agency.lang or ""]],
        ),
        "stops.txt": _csv(
            ("stop_id", "stop_name", "stop_lat", "stop_lon"),
            [
                [stop.stop_id, stop.display_name(), f"{lat:.6f}", f"{lon:.6f}"]
                for stop, (lon, lat) in _stop_positions(stops)
            ],
        ),
        "routes.txt": _csv(
            ("route_id", "agency_id", "route_short_name", "route_long_name", "route_type"),
            route_rows,
        ),
        "trips.txt": _csv(
            ("route_id", "service_id", "trip_id", "trip_headsign", "shape_id"), trip_rows
        ),
        "stop_times.txt": _csv(
            (
                "trip_id",
                "arrival_time",
                "departure_time",
                "stop_id",
                "stop_sequence",
                "shape_dist_traveled",
                "timepoint",
            ),
            stop_time_rows,
        ),
        "shapes.txt": _csv(
            (
                "shape_id",
                "shape_pt_lat",
                "shape_pt_lon",
                "shape_pt_sequence",
                "shape_dist_traveled",
            ),
            shape_rows,
        ),
        "calendar.txt": _csv(
            (
                "service_id",
                "monday",
                "tuesday",
                "wednesday",
                "thursday",
                "friday",
                "saturday",
                "sunday",
                "start_date",
                "end_date",
            ),
            [[SERVICE_ID, 1, 1, 1, 1, 1, 1, 1, _date(start_date), _date(end_date)]],
        ),
        "frequencies.txt": _csv(
            ("trip_id", "start_time", "end_time", "headway_secs", "exact_times"),
            frequency_rows,
        ),
    }

    logger.info(
        "Built a feed with %d route(s), %d stop(s) and %d frequency row(s).",
        len(route_rows),
        len(stops),
        len(frequency_rows),
    )
    return Feed(files=files)


def _shape_points(geom: LineString, tolerance_m: float) -> list[tuple[float, float, float]]:
    """`(lon, lat, distance_m)` per shape vertex, distance strictly increasing.

    The line is simplified in metres and only then reprojected, so the vertices
    written here and the stop distances measured by `_stop_time_rows` come off
    the same geometry — `shape_dist_traveled` is meaningless if the two files
    measure along different lines.

    Vertices that do not advance the distance by `DIST_EPSILON_M` are dropped.
    A matched trace can repeat a coordinate where two OSRM legs were joined, and
    two vertices with equal `shape_dist_traveled` are a validator error.
    """
    simplified = _simplify(geom, tolerance_m)
    metric = shapely.get_coordinates(simplified)
    lonlat = shapely.get_coordinates(_to_wgs84(simplified))

    points: list[tuple[float, float, float]] = []
    travelled = 0.0
    for index, (x, y) in enumerate(metric):
        if index:
            previous_x, previous_y = metric[index - 1]
            travelled += hypot(x - previous_x, y - previous_y)
            if travelled - points[-1][2] < DIST_EPSILON_M:
                continue
        points.append((float(lonlat[index][0]), float(lonlat[index][1]), travelled))

    return points


def _stop_time_rows(
    route: ExportRoute, tolerance_m: float, speed_mps: float
) -> list[Sequence[object]]:
    """The reference trip: one row per stop, timed from the trip's own start.

    Times are offsets, and the trip is anchored at 00:00:00. For a
    frequency-based trip that is what `stop_times.txt` means — the actual clock
    times of a departure are `frequencies.start_time` plus these offsets — and
    anchoring at midnight makes the offsets readable as durations.

    Each stop's offset is its distance along the route over the route's observed
    operating speed. No separate dwell allowance is added, because the speed is
    wall-clock over the whole ride and already includes every stop the kombi
    made; adding one would count the same seconds twice.

    Both the times and the distances are forced strictly increasing after
    rounding. Ordering comes from `route_stops.seq`, which `cluster.py` already
    sorted by distance along the canonical line, so this only ever moves a value
    by a rounding step — but two stops within 5 m of each other, or either side
    of a simplified corner, would otherwise print equal values, which the
    validator rejects.
    """
    simplified = _simplify(route.geom, tolerance_m)

    distances = _increasing(
        [
            round(float(shapely.line_locate_point(simplified, stop.centroid)), 2)
            for stop in route.stops
        ],
        step=DIST_EPSILON_M,
    )
    seconds = _increasing([round(distance / speed_mps) for distance in distances], step=1)

    return [
        [
            route.trip_id,
            format_time(int(offset)),
            format_time(int(offset)),
            stop.stop_id,
            sequence,
            f"{distance:.2f}",
            0,  # timepoint: approximate, which is all a kombi time can be.
        ]
        for sequence, (stop, distance, offset) in enumerate(
            zip(route.stops, distances, seconds, strict=True), start=1
        )
    ]


def _simplify(geom: LineString, tolerance_m: float) -> LineString:
    """Douglas–Peucker in metres, guarding against a degenerate result."""
    if tolerance_m <= 0:
        return geom
    simplified = geom.simplify(tolerance_m, preserve_topology=False)
    return simplified if len(simplified.coords) >= 2 else geom


def _increasing(values: Sequence[float], *, step: float) -> list[float]:
    """Force a non-decreasing sequence strictly increasing by at least `step`."""
    forced: list[float] = []
    for value in values:
        if forced and value < forced[-1] + step:
            value = round(forced[-1] + step, 2)
        forced.append(value)
    return forced


def _stop_positions(
    stops: dict[int, ExportStop],
) -> list[tuple[ExportStop, tuple[float, float]]]:
    """Stops in id order, each with its EPSG:4326 `(lon, lat)`.

    Reprojected in one batch: `pyproj` builds a transformer per call, and doing
    it per stop is measurably slower on a real network.
    """
    ordered = [stops[stop_id] for stop_id in sorted(stops)]
    if not ordered:
        return []

    projected = gpd.GeoSeries([stop.centroid for stop in ordered], crs=UTM36S).to_crs(WGS84)
    return [
        (stop, (float(point.x), float(point.y)))
        for stop, point in zip(ordered, projected, strict=True)
    ]


def _service_window(
    routes: Sequence[ExportRoute], zone: ZoneInfo, today: date, service_days: int
) -> tuple[date, date]:
    """`(start_date, end_date)` for `calendar.txt`.

    Service starts on the day of the earliest observed ride — the feed should
    not claim service before anyone saw any — and runs `service_days` from
    today, so that a feed exported from months-old traces is still current
    rather than born expired.
    """
    departures = [moment for route in routes for moment in route.departures]
    start = min(moment.astimezone(zone).date() for moment in departures) if departures else today
    return min(start, today), today + timedelta(days=service_days)


def format_time(seconds: int) -> str:
    """Seconds since the service day's start as GTFS `HH:MM:SS`.

    Hours past 24 are legal GTFS and are what a service running past midnight
    looks like, so the hour field is not wrapped.
    """
    hours, remainder = divmod(int(seconds), 3600)
    minutes, second = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{second:02d}"


def _date(value: date) -> str:
    return value.strftime("%Y%m%d")


def _csv(header: Sequence[str], rows: Iterable[Sequence[object]]) -> str:
    """One CSV file as text: UTF-8, no BOM, LF line endings, minimal quoting."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue()


# --------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------


def export(
    path: str | Path | None = None,
    engine: Engine | None = None,
    **build_kwargs: Any,
) -> Path:
    """Load the network, build the feed, write the zip. Returns the path written.

    `**build_kwargs` are forwarded to `build_feed`.
    """
    destination = Path(path) if path is not None else Path(settings.gtfs_path)
    routes = load_export_network(engine)
    feed = build_feed(routes, **build_kwargs)
    written = feed.write(destination)
    logger.info("Wrote %s (%.1f KiB).", written, written.stat().st_size / 1024)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "-o",
        "--out",
        default=None,
        help=f"where to write the feed (default {settings.gtfs_path})",
    )
    parser.add_argument(
        "--fill-bands",
        action="store_true",
        help="publish every time band, not only the ones with observed departures",
    )
    parser.add_argument(
        "--default-headway-s",
        type=int,
        default=DEFAULT_HEADWAY_S,
        help=f"headway where nothing was observed (default {DEFAULT_HEADWAY_S})",
    )
    parser.add_argument(
        "--service-days",
        type=int,
        default=DEFAULT_SERVICE_DAYS,
        help=f"length of the calendar.txt service window (default {DEFAULT_SERVICE_DAYS})",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    routes = load_export_network()
    try:
        feed = build_feed(
            routes,
            fill_unobserved_bands=args.fill_bands,
            default_headway_s=args.default_headway_s,
            service_days=args.service_days,
        )
    except EmptyFeedError as error:
        print(f"error: {error}")
        return 1

    destination = feed.write(args.out if args.out else settings.gtfs_path)
    print(f"Wrote {destination} ({destination.stat().st_size / 1024:.1f} KiB)")

    zone = Agency.from_settings().zone
    for route in routes:
        if len(route.stops) < 2:
            continue
        headways = estimate_headways(route.departures, zone, fill_unobserved_bands=args.fill_bands)
        measured = sum(1 for headway in headways if headway.measured)
        print(
            f"  route {route.route_id}: {len(route.stops)} stop(s), "
            f"{len(route.departures)} observed departure(s), "
            f"{len(headways)} band(s), {measured} measured"
        )
        for headway in headways:
            print(
                f"    {headway.band.name:<14} "
                f"{format_time(headway.band.start_s)}-{format_time(headway.band.end_s)} "
                f"every {headway.headway_s // 60} min "
                f"({headway.basis}, {headway.departures} departure(s))"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
