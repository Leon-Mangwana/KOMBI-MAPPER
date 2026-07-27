"""Map matching: snap a cleaned trace onto the road network with OSRM.

A cleaned trace is smooth but it still floats beside the road — a Kalman filter
knows nothing about where the tarmac is. Map matching pins the trace to actual
road geometry, which is what makes two traces comparable: two contributors on
the same kombi route produce the same sequence of roads, even though their raw
points never coincide.

Input is a *cleaned* trace in EPSG:32736 — see `clean.py`. Output is a single
LineString written to `traces.matched_geom` in EPSG:4326. All stitching
arithmetic below (projections, cuts, lengths) happens in EPSG:32736 and is
reprojected only on the way into the database; nothing here measures a distance
in degrees. See ../CLAUDE.md.

Single LineString, not multipart, deliberately: the column is typed
`LINESTRING`, and PostGIS rejects a MULTILINESTRING outright. Everything below
therefore works towards one connected line.

### Chunking

`osrm-routed` caps a match request at `--max-matching-size` coordinates — 100 by
default — and answers `TooBig` past that, so a 40-minute trace at 1 Hz has to be
split across requests. Chunks *overlap*: points near a seam are sent twice, once
with road context to their left and once with context to their right, and each
chunk keeps only the half of the overlap where it had context on both sides.
Without the overlap every seam would be a request boundary at which the matcher
was working blind, and seams would reliably be where the matched line jumped a
block.

Chunks are stitched by index, not by geometry. Each response's `tracepoints`
array maps input point → snapped location one-for-one, so each matched stretch
is cut (in metres) at the snapped locations of the first and last input point
the chunk owns, and the cut pieces are concatenated in trace order. This is why
`tidy` is never enabled: tidying drops input points, and the one-for-one index
mapping goes with it.

### Failure

OSRM answers `NoMatch` when a stretch of trace has no plausible road under it —
which in Harare is routine, not exceptional: kombis use unpaved shortcuts and
new roads that OSM has not caught up with. Failing the trace would throw away
the parts that *did* match, so instead the unmatched stretch falls back to the
cleaned geometry, joined to the matched pieces either side, and is logged and
reported on `MatchResult.segments`. Downstream stages can then decide for
themselves — `matched_fraction` is the number to gate on.

That tolerance is only extended to genuine match failures (`NoMatch`,
`NoSegment`). A `TooBig`, an unparseable body, a 5xx or a connection error is a
bug or an outage, not a road that does not exist, and raises `OSRMError`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import geopandas as gpd
import httpx
import numpy as np
import pandas as pd
from shapely.geometry import LineString, Point
from shapely.ops import substring
from sqlalchemy import Engine, text

from clean import UTM36S, WGS84, clean_trace, load_trace
from config import get_engine, settings

logger = logging.getLogger(__name__)

#: Coordinates per `/match` request. `osrm-routed --max-matching-size` defaults
#: to 100 and answers `TooBig` above it. Raise this only together with the flag
#: in ../infra/docker-compose.yml.
MAX_COORDINATES_PER_REQUEST = 100

#: Points shared between consecutive chunks; each chunk keeps half of them. Ten
#: points is ~10 s of road at 1 Hz — long enough for the matcher to have
#: committed to a road before it reaches the seam.
OVERLAP_POINTS = 10

#: `radiuses` bounds, in metres. OSRM's own default is 5 m, which is optimistic
#: for a phone in a minibus: too tight a radius turns an ordinary 20 m error
#: into a `NoSegment`. Too loose is not free either — the matcher then has more
#: candidate roads to choose between, and picks wrong more often.
DEFAULT_RADIUS_M = 10.0
MIN_RADIUS_M = 5.0
MAX_RADIUS_M = 50.0

#: OSRM `code` values meaning "no road here", as opposed to "bad request". Only
#: these fall back to the cleaned geometry.
FALLBACK_CODES = frozenset({"NoMatch", "NoSegment"})

DEFAULT_PROFILE = "driving"
REQUEST_TIMEOUT_S = 30.0

_GAP_REASON = "OSRM matched no road for these points"


class OSRMError(RuntimeError):
    """OSRM was unreachable, or answered something that is not a match failure."""


@dataclass(frozen=True)
class MatchedSegment:
    """One contiguous stretch of the output geometry, and where it came from.

    Attributes:
        geometry: The stretch, in EPSG:32736.
        start_index: First input point the stretch spans, inclusive.
        end_index: Last input point the stretch spans, inclusive.
        matched: True if OSRM matched it; False if it is cleaned-geometry
            fallback.
        confidence: OSRM's confidence in the matching this came from, 0–1.
            `None` for fallbacks.
        reason: Why this stretch fell back. `None` when `matched`.

    A fallback runs *between* the matched pieces either side of it, so it starts
    and ends at their endpoints and consecutive segments share an index.
    """

    geometry: LineString
    start_index: int
    end_index: int
    matched: bool
    confidence: float | None = None
    reason: str | None = None

    @property
    def length_m(self) -> float:
        return float(self.geometry.length)


@dataclass(frozen=True)
class MatchResult:
    """A matched trace: one geometry, plus an account of how it was arrived at.

    Attributes:
        geometry: The stitched line in EPSG:32736, spanning the whole trace.
        segments: The stretches it was assembled from, in trace order.
    """

    geometry: LineString
    segments: tuple[MatchedSegment, ...]

    @property
    def length_m(self) -> float:
        return float(self.geometry.length)

    @property
    def matched_length_m(self) -> float:
        return sum(s.length_m for s in self.segments if s.matched)

    @property
    def matched_fraction(self) -> float:
        """Share of the output length that OSRM actually matched, 0–1.

        The number to gate on: a trace that came back 30% matched has either
        left the road network or been recorded badly, and should not be fed to
        route clustering without a look.
        """
        total = sum(s.length_m for s in self.segments)
        return 1.0 if total == 0.0 else self.matched_length_m / total

    @property
    def fully_matched(self) -> bool:
        return all(s.matched for s in self.segments)

    @property
    def min_confidence(self) -> float | None:
        """Lowest confidence OSRM reported, or `None` if nothing matched."""
        scores = [s.confidence for s in self.segments if s.confidence is not None]
        return min(scores) if scores else None

    def to_wgs84(self) -> LineString:
        """The geometry reprojected to EPSG:4326, as stored."""
        return gpd.GeoSeries([self.geometry], crs=UTM36S).to_crs(WGS84).iloc[0]


def match_trace(
    gdf: gpd.GeoDataFrame,
    *,
    osrm_url: str | None = None,
    profile: str = DEFAULT_PROFILE,
    max_coordinates: int = MAX_COORDINATES_PER_REQUEST,
    overlap: int = OVERLAP_POINTS,
    default_radius_m: float = DEFAULT_RADIUS_M,
    max_radius_m: float = MAX_RADIUS_M,
    use_timestamps: bool = True,
    gaps: str = "split",
    client: httpx.Client | None = None,
    timeout_s: float = REQUEST_TIMEOUT_S,
) -> MatchResult:
    """Map-match a cleaned trace against OSRM and return the stitched line.

    The trace is chunked to `max_coordinates` points per request with `overlap`
    points shared across each seam, every chunk is POSTed— see the note below —
    to `/match/v1/{profile}`, and the matched pieces are cut and concatenated
    into one LineString spanning the whole trace. Stretches OSRM could not match
    are filled with the cleaned geometry and flagged on the result rather than
    aborting the trace.

    **On POST.** `osrm-routed` serves GET only: coordinates go in the URL path,
    and there is no request-body form of the API to POST to. Requests here are
    therefore `GET /match/v1/{profile}/{lon},{lat};...`. This is a property of
    the OSRM HTTP server, not a choice — the alternative would be dropping
    `osrm-routed` for the NodeJS or C++ bindings, which take coordinates as
    arrays in-process. Chunking keeps URLs to roughly 4 KB, well inside what
    `osrm-routed` accepts.

    Per-point `radiuses` are derived from the device's reported `accuracy_m`,
    so a fix the phone already knew was poor is matched loosely rather than
    being forced onto the nearest road. Per-point `timestamps` are sent when
    available, which lets OSRM reason about speed between candidates — the
    single biggest quality lever on a noisy urban trace.

    Args:
        gdf: Cleaned trace in EPSG:32736, ordered, with an optional
            `accuracy_m` column and an optional `recorded_at` column.
        osrm_url: OSRM base URL. Defaults to `settings.osrm_url`.
        profile: OSRM profile name; must be the one the dataset was prepared
            with. `driving` for kombis.
        max_coordinates: Coordinates per request. Must not exceed the server's
            `--max-matching-size`.
        overlap: Points shared between consecutive chunks.
        default_radius_m: Search radius for points with no reported accuracy.
        max_radius_m: Ceiling on the per-point radius.
        use_timestamps: Send `timestamps`. Ignored if the trace has no
            `recorded_at`, or if two points share a second once rounded — OSRM
            requires strictly increasing values.
        gaps: OSRM's `gaps` parameter. The default `split` breaks the matching
            at large time gaps, which is what you want: a trace with a
            five-minute hole in it should not be matched straight across. The
            break then bridges as a fallback segment like any other.
        client: An `httpx.Client` to reuse. One is created and closed per call
            if omitted. Tests inject an `httpx.MockTransport` through this.
        timeout_s: Per-request timeout, when creating the client here.

    Returns:
        A `MatchResult` whose `geometry` spans the whole trace.

    Raises:
        ValueError: The trace has fewer than two points, has no CRS, or the
            chunking arguments are inconsistent.
        OSRMError: OSRM was unreachable or answered with something other than a
            match failure.
    """
    count = len(gdf)
    if count < 2:
        raise ValueError(f"Need at least two points to match a trace; got {count}.")
    if gdf.crs is None:
        raise ValueError("Trace has no CRS; expected EPSG:32736 from clean.clean_trace.")
    if max_coordinates < 2:
        raise ValueError(f"max_coordinates must be at least 2; got {max_coordinates}.")
    if not 0 <= overlap < max_coordinates:
        raise ValueError(f"overlap must be in [0, {max_coordinates}); got {overlap}.")

    metric = gdf.to_crs(UTM36S)
    metric_xy = np.column_stack(
        [metric.geometry.x.to_numpy(dtype=float), metric.geometry.y.to_numpy(dtype=float)]
    )

    lonlat = gdf.to_crs(WGS84)
    lons = lonlat.geometry.x.to_numpy(dtype=float)
    lats = lonlat.geometry.y.to_numpy(dtype=float)

    radiuses = _radiuses(gdf, default_radius_m=default_radius_m, max_radius_m=max_radius_m)
    timestamps = _timestamps(gdf) if use_timestamps else None

    base_url = (osrm_url if osrm_url is not None else settings.osrm_url).rstrip("/")
    chunks = _plan_chunks(count, max_coordinates=max_coordinates, overlap=overlap)

    pieces: list[_Piece] = []
    reasons: list[str | None] = [None] * count

    owned_client = client is None
    session = client if client is not None else httpx.Client(timeout=timeout_s)
    try:
        for chunk in chunks:
            window = slice(chunk.context_start, chunk.context_end + 1)
            payload, failure = _request_match(
                session,
                base_url=base_url,
                profile=profile,
                lons=lons[window],
                lats=lats[window],
                radiuses=radiuses[window],
                timestamps=None if timestamps is None else timestamps[window],
                gaps=gaps,
            )
            if payload is None:
                logger.warning(
                    "OSRM returned %s for trace points %d-%d; falling back to cleaned geometry.",
                    failure,
                    chunk.owned_start,
                    chunk.owned_end,
                )
                reason = f"OSRM returned {failure} for these points"
                for index in range(chunk.owned_start, chunk.owned_end + 1):
                    reasons[index] = reason
                continue

            pieces.extend(_pieces_from_payload(payload, chunk))
    finally:
        if owned_client:
            session.close()

    segments = _assemble(pieces, metric_xy=metric_xy, reasons=reasons)
    result = MatchResult(geometry=_join(segments), segments=tuple(segments))

    if not result.fully_matched:
        logger.warning(
            "Matched %.1f%% of a %.0f m trace; %d stretch(es) fell back to cleaned geometry.",
            result.matched_fraction * 100.0,
            result.length_m,
            sum(1 for s in result.segments if not s.matched),
        )
    return result


def store_matched_geom(
    trace_id: int, result: MatchResult | LineString, engine: Engine | None = None
) -> None:
    """Write a matched line to `traces.matched_geom`, reprojected to EPSG:4326.

    Accepts a `MatchResult` or a bare LineString; a bare one is assumed to be in
    EPSG:32736, like everything else the pipeline passes around.

    Raises `LookupError` if no such trace exists — silently updating zero rows
    would let a whole batch report success having stored nothing.
    """
    if isinstance(result, MatchResult):
        geometry = result.to_wgs84()
    else:
        geometry = gpd.GeoSeries([result], crs=UTM36S).to_crs(WGS84).iloc[0]

    engine = engine if engine is not None else get_engine()
    with engine.begin() as conn:
        updated = conn.execute(
            _UPDATE_MATCHED_GEOM_SQL, {"trace_id": trace_id, "wkt": geometry.wkt}
        )
        if updated.rowcount == 0:
            raise LookupError(f"Trace {trace_id} does not exist.")


def match_stored_trace(
    trace_id: int, engine: Engine | None = None, **match_kwargs: object
) -> MatchResult:
    """Load a trace, clean it, match it, and store the result. Returns the match.

    The end-to-end convenience path. Keyword arguments are forwarded to
    `match_trace`; call the three steps yourself when you want the cleaned frame
    for something else too, since this throws it away.
    """
    cleaned = clean_trace(load_trace(trace_id, engine=engine))
    result = match_trace(cleaned, **match_kwargs)  # type: ignore[arg-type]
    store_matched_geom(trace_id, result, engine=engine)
    return result


_UPDATE_MATCHED_GEOM_SQL = text(
    """
    UPDATE traces
    SET matched_geom = ST_GeomFromText(:wkt, 4326)
    WHERE id = :trace_id
    """
)


@dataclass(frozen=True)
class _Chunk:
    """One request: `context` points are sent, `owned` points are kept.

    The context outside the owned range exists only so the matcher has road
    either side of the seam. Owned ranges cover the trace without overlapping,
    which is what keeps the stitched output from doubling back — except that
    consecutive chunks *share* their boundary point. Sharing it is what makes
    the seam join: both chunks cut their geometry at the same input point, so
    the two pieces meet there instead of leaving a one-point hole to bridge.
    """

    context_start: int
    context_end: int
    owned_start: int
    owned_end: int


@dataclass(frozen=True)
class _Piece:
    """A matched stretch of road, already cut down to one chunk's owned range."""

    geometry: LineString
    start_index: int
    end_index: int
    confidence: float | None


def _plan_chunks(count: int, *, max_coordinates: int, overlap: int) -> list[_Chunk]:
    """Split `count` points into overlapping requests with owned ranges.

    The seam between two chunks is put in the *middle* of their overlap, since
    that is the point best served by both: the left chunk saw road ahead of it
    and the right chunk saw road behind it. Owning up to the edge of the overlap
    instead would keep exactly the half that was matched blind.

    The seam point itself is owned by both sides, so their matched pieces are cut
    at the same input point and meet there.

    The last chunk is pulled back to full size rather than being left as a short
    tail, because a 4-point request matches poorly and the tail of a trace is
    where the terminus is.
    """
    if count <= max_coordinates:
        return [_Chunk(0, count - 1, 0, count - 1)]

    stride = max_coordinates - overlap
    starts = list(range(0, count - max_coordinates + 1, stride))
    if starts[-1] != count - max_coordinates:
        starts.append(count - max_coordinates)

    chunks: list[_Chunk] = []
    owned_start = 0
    for position, start in enumerate(starts):
        end = start + max_coordinates - 1
        if position + 1 < len(starts):
            next_start = starts[position + 1]
            # Middle of the overlap [next_start, end], and always at least one
            # point ahead of where this chunk starts owning.
            seam = next_start + (end - next_start + 1) // 2
            owned_end = max(seam, owned_start + 1)
        else:
            owned_end = count - 1
        chunks.append(_Chunk(start, end, owned_start, owned_end))
        owned_start = owned_end

    return chunks


def _radiuses(gdf: gpd.GeoDataFrame, *, default_radius_m: float, max_radius_m: float) -> np.ndarray:
    """Per-point search radius, from the device's own accuracy where reported."""
    if "accuracy_m" in gdf.columns:
        radiuses = pd.Series(gdf["accuracy_m"]).to_numpy(dtype=float)
        radiuses = np.where(np.isnan(radiuses), default_radius_m, radiuses)
    else:
        radiuses = np.full(len(gdf), default_radius_m, dtype=float)
    return np.clip(radiuses, MIN_RADIUS_M, max_radius_m)


def _timestamps(gdf: gpd.GeoDataFrame) -> np.ndarray | None:
    """Unix seconds per point, or `None` if they cannot be sent.

    OSRM requires strictly increasing timestamps. A trace sampled faster than
    1 Hz collapses to duplicate seconds once rounded, so rather than distorting
    the times to satisfy the server, the parameter is dropped for that trace and
    the matching proceeds without it.
    """
    if "recorded_at" not in gdf.columns:
        return None

    stamps = pd.to_datetime(pd.Series(gdf["recorded_at"]).reset_index(drop=True), utc=True)
    if stamps.isna().any():
        return None

    epoch = (stamps - pd.Timestamp("1970-01-01", tz="UTC")).dt.total_seconds()
    seconds = np.rint(epoch.to_numpy(dtype=float)).astype(np.int64)
    if np.any(np.diff(seconds) <= 0):
        logger.debug("Trace timestamps are not strictly increasing per second; omitting them.")
        return None
    return seconds


def _request_match(
    client: httpx.Client,
    *,
    base_url: str,
    profile: str,
    lons: np.ndarray,
    lats: np.ndarray,
    radiuses: np.ndarray,
    timestamps: np.ndarray | None,
    gaps: str,
) -> tuple[dict | None, str | None]:
    """Match one chunk. Returns `(payload, None)`, or `(None, code)` on NoMatch.

    Six decimal places on the coordinates is ~0.11 m, comfortably finer than any
    GPS fix and short enough to keep the URL small.
    """
    coordinates = ";".join(f"{lon:.6f},{lat:.6f}" for lon, lat in zip(lons, lats, strict=True))
    params = {
        "geometries": "geojson",
        "overview": "full",
        "steps": "false",
        "annotations": "false",
        # Never `true`: tidying drops input points, and the one-to-one mapping
        # from input index to tracepoint that the stitching relies on with it.
        "tidy": "false",
        "gaps": gaps,
        "radiuses": ";".join(f"{radius:.1f}" for radius in radiuses),
    }
    if timestamps is not None:
        params["timestamps"] = ";".join(str(int(value)) for value in timestamps)

    url = f"{base_url}/match/v1/{profile}/{coordinates}"
    try:
        response = client.get(url, params=params)
    except httpx.HTTPError as exc:
        raise OSRMError(f"Request to OSRM at {base_url} failed: {exc}") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise OSRMError(
            f"OSRM returned HTTP {response.status_code} with a body that is not JSON."
        ) from exc

    if not isinstance(payload, dict):
        raise OSRMError(f"OSRM returned a {type(payload).__name__}, expected an object.")

    code = payload.get("code")
    if code == "Ok" and response.status_code == 200:
        return payload, None
    if code in FALLBACK_CODES:
        return None, str(code)

    raise OSRMError(f"OSRM answered HTTP {response.status_code} {code}: {payload.get('message')}")


def _pieces_from_payload(payload: dict, chunk: _Chunk) -> list[_Piece]:
    """Cut a chunk's matchings down to the points the chunk owns.

    A response holds one matching per stretch OSRM could follow without a break,
    and a `tracepoints` entry per input coordinate saying which matching it
    landed on and where. Grouping the tracepoints by matching therefore gives,
    for each matching, the input points it covers; the geometry is then cut
    between the snapped locations of the first and last of those the chunk owns.

    A `null` tracepoint mid-matching is an outlier OSRM ignored — the matching
    still runs through it, so it needs no fallback. `null`s at the edges, and
    the ground between two matchings, do, and are left for `_assemble` to fill.
    """
    matchings = payload.get("matchings") or []
    tracepoints = payload.get("tracepoints") or []

    grouped: dict[int, list[int]] = {}
    snapped: dict[int, tuple[float, float]] = {}
    for offset, tracepoint in enumerate(tracepoints):
        if not isinstance(tracepoint, dict):
            continue
        location = tracepoint.get("location")
        matching_index = tracepoint.get("matchings_index")
        if location is None or matching_index is None:
            continue
        index = chunk.context_start + offset
        grouped.setdefault(int(matching_index), []).append(index)
        snapped[index] = (float(location[0]), float(location[1]))

    pieces: list[_Piece] = []
    for matching_index, indices in sorted(grouped.items()):
        kept = [i for i in indices if chunk.owned_start <= i <= chunk.owned_end]
        if len(kept) < 2:
            # One owned point spans no ground; the bridge either side covers it.
            continue

        try:
            matching = matchings[matching_index]
        except (IndexError, TypeError):
            logger.warning(
                "OSRM tracepoint referenced matching %d, which is absent.", matching_index
            )
            continue

        coordinates = (matching.get("geometry") or {}).get("coordinates") or []
        if len(coordinates) < 2:
            continue

        line = LineString(_to_metric([c[0] for c in coordinates], [c[1] for c in coordinates]))
        ends = _to_metric(
            [snapped[kept[0]][0], snapped[kept[-1]][0]],
            [snapped[kept[0]][1], snapped[kept[-1]][1]],
        )
        start_distance = line.project(Point(ends[0]))
        end_distance = line.project(Point(ends[1]))
        if end_distance - start_distance <= 0.0:
            continue

        cut = substring(line, start_distance, end_distance)
        if not isinstance(cut, LineString) or len(cut.coords) < 2:
            continue

        confidence = matching.get("confidence")
        pieces.append(
            _Piece(
                geometry=cut,
                start_index=kept[0],
                end_index=kept[-1],
                confidence=None if confidence is None else float(confidence),
            )
        )

    return pieces


def _assemble(
    pieces: list[_Piece], *, metric_xy: np.ndarray, reasons: list[str | None]
) -> list[MatchedSegment]:
    """Order the matched pieces and bridge every gap between them.

    Every input point ends up covered: by a matched piece, or by a fallback that
    runs the cleaned geometry from the end of one piece to the start of the next.

    Pieces meeting exactly at a shared index are joined directly — that is a
    chunk seam, and both sides cut at the same point. A gap of even zero points
    is bridged, though: two matchings that are adjacent by index still leave a
    hole in the *line* where OSRM broke off, and the output has to be one
    connected LineString.
    """
    count = len(metric_xy)
    ordered = sorted(pieces, key=lambda piece: (piece.start_index, piece.end_index))

    segments: list[MatchedSegment] = []
    previous_index: int | None = None
    previous_point: tuple[float, float] | None = None

    for piece in ordered:
        if previous_index is not None and piece.start_index < previous_index:
            # Owned ranges only ever share their boundary point, so a real
            # overlap means OSRM interleaved two matchings over the same points.
            # Keeping both would double back.
            logger.warning(
                "Discarding a matched stretch over points %d-%d; it overlaps the previous one.",
                piece.start_index,
                piece.end_index,
            )
            continue

        if previous_index is None or piece.start_index > previous_index:
            interior_start = 0 if previous_index is None else previous_index + 1
            span_start = 0 if previous_index is None else previous_index
            bridge = _fallback_segment(
                interior_start,
                piece.start_index - 1,
                metric_xy=metric_xy,
                before=previous_point,
                after=piece.geometry.coords[0],
                span=(span_start, piece.start_index),
                reasons=reasons,
            )
            if bridge is not None:
                segments.append(bridge)

        segments.append(
            MatchedSegment(
                geometry=piece.geometry,
                start_index=piece.start_index,
                end_index=piece.end_index,
                matched=True,
                confidence=piece.confidence,
            )
        )
        previous_index = piece.end_index
        previous_point = piece.geometry.coords[-1]

    if previous_index is None:
        logger.warning("OSRM matched none of this trace; using the cleaned geometry throughout.")
        tail = _fallback_segment(
            0,
            count - 1,
            metric_xy=metric_xy,
            before=None,
            after=None,
            span=(0, count - 1),
            reasons=reasons,
        )
    elif previous_index < count - 1:
        tail = _fallback_segment(
            previous_index + 1,
            count - 1,
            metric_xy=metric_xy,
            before=previous_point,
            after=None,
            span=(previous_index, count - 1),
            reasons=reasons,
        )
    else:
        tail = None

    if tail is not None:
        segments.append(tail)

    return segments


def _fallback_segment(
    interior_start: int,
    interior_end: int,
    *,
    metric_xy: np.ndarray,
    before: tuple[float, float] | None,
    after: tuple[float, float] | None,
    span: tuple[int, int],
    reasons: list[str | None],
) -> MatchedSegment | None:
    """Cleaned geometry over `interior`, anchored to the matched pieces either side.

    The anchors replace the cleaned positions of the boundary points rather than
    being added to them: point `n` snapped onto a road is a better start for the
    bridge than point `n` floating beside it, and using both would put a spur in
    the line. Returns `None` when there is nothing to bridge.
    """
    coordinates: list[tuple[float, float]] = []
    if before is not None:
        coordinates.append((float(before[0]), float(before[1])))
    if interior_end >= interior_start:
        coordinates.extend(
            (float(x), float(y)) for x, y in metric_xy[interior_start : interior_end + 1]
        )
    if after is not None:
        coordinates.append((float(after[0]), float(after[1])))

    coordinates = _dedupe(coordinates)
    if len(coordinates) < 2:
        return None

    reason = next(
        (
            reasons[index]
            for index in range(max(interior_start, 0), min(interior_end, len(reasons) - 1) + 1)
            if reasons[index] is not None
        ),
        _GAP_REASON,
    )
    return MatchedSegment(
        geometry=LineString(coordinates),
        start_index=span[0],
        end_index=span[1],
        matched=False,
        reason=reason,
    )


def _join(segments: list[MatchedSegment]) -> LineString:
    """Concatenate the segments into one line, dropping repeated join points."""
    coordinates: list[tuple[float, float]] = []
    for segment in segments:
        coordinates.extend((float(x), float(y)) for x, y in segment.geometry.coords)

    coordinates = _dedupe(coordinates)
    if len(coordinates) < 2:
        raise OSRMError("Assembled a matched geometry with fewer than two distinct points.")
    return LineString(coordinates)


def _dedupe(coordinates: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Drop consecutive duplicates, which joins introduce at every seam."""
    deduped: list[tuple[float, float]] = []
    for coordinate in coordinates:
        if not deduped or coordinate != deduped[-1]:
            deduped.append(coordinate)
    return deduped


def _to_metric(lons: list[float], lats: list[float]) -> np.ndarray:
    """Reproject WGS 84 lon/lat to EPSG:32736 as an (n, 2) array of metres."""
    series = gpd.GeoSeries(gpd.points_from_xy(lons, lats), crs=WGS84).to_crs(UTM36S)
    return np.column_stack([series.x.to_numpy(dtype=float), series.y.to_numpy(dtype=float)])
