"""Serving the canonical network — the routes and stops the pipeline clustered.

These are what the public map in `/web` draws, and the shape of the responses
follows from that: each endpoint answers with a **GeoJSON FeatureCollection**,
which MapLibre accepts as a source body verbatim. Anything else would mean the
browser reshaping the payload before it could be drawn.

The tables here are written by `pipeline/cluster.py` and only read by the API —
the reverse of `traces`. A response therefore describes the last clustering run,
and is stable until the next one.

Lengths are computed in EPSG:32736, never in degrees. See ../../CLAUDE.md.
"""

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Route, RouteStop, Stop, Trace

router = APIRouter(tags=["network"])

#: Metric CRS for Harare. Lengths in degrees are meaningless — see CLAUDE.md.
UTM36S = 32736


@router.get("/routes")
def list_routes(
    db: Session = Depends(get_db),
    include_geometry: bool = Query(
        True,
        description="Set false for a light index of the network, without the line geometry.",
    ),
) -> dict[str, Any]:
    """Every canonical route, as a GeoJSON FeatureCollection.

    The whole network is a few dozen routes for one city, so this is unpaged: a
    map draws all of them at once, and splitting that over pages would only mean
    the client reassembling them.
    """
    stop_count = (
        select(func.count())
        .select_from(RouteStop)
        .where(RouteStop.route_id == Route.id)
        .scalar_subquery()
    )
    trace_count = (
        select(func.count()).select_from(Trace).where(Trace.route_id == Route.id).scalar_subquery()
    )

    rows = db.execute(
        select(
            Route.id,
            Route.short_name,
            Route.long_name,
            func.ST_AsGeoJSON(Route.geom),
            func.ST_Length(func.ST_Transform(Route.geom, UTM36S)),
            stop_count,
            trace_count,
        ).order_by(Route.id)
    ).all()

    features = [_route_feature(row, include_geometry=include_geometry, stops=None) for row in rows]
    return {"type": "FeatureCollection", "features": features}


@router.get("/routes/{route_id}")
def get_route(route_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    """One route as a GeoJSON Feature, with its stops in travel order.

    The stops ride along in `properties.stops` rather than being a second
    request: a route is only meaningful with them, and the list is short.
    """
    stop_count = (
        select(func.count())
        .select_from(RouteStop)
        .where(RouteStop.route_id == Route.id)
        .scalar_subquery()
    )
    trace_count = (
        select(func.count()).select_from(Trace).where(Trace.route_id == Route.id).scalar_subquery()
    )

    row = db.execute(
        select(
            Route.id,
            Route.short_name,
            Route.long_name,
            func.ST_AsGeoJSON(Route.geom),
            func.ST_Length(func.ST_Transform(Route.geom, UTM36S)),
            stop_count,
            trace_count,
        ).where(Route.id == route_id)
    ).first()

    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Route not found.")

    stops = db.execute(
        select(RouteStop.seq, Stop.id, Stop.name, func.ST_X(Stop.geom), func.ST_Y(Stop.geom))
        .join(Stop, Stop.id == RouteStop.stop_id)
        .where(RouteStop.route_id == route_id)
        .order_by(RouteStop.seq)
    ).all()

    ordered = [
        {"seq": seq, "id": stop_id, "name": name, "lon": lon, "lat": lat}
        for seq, stop_id, name, lon, lat in stops
    ]
    return _route_feature(row, include_geometry=True, stops=ordered)


@router.get("/stops")
def list_stops(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Every stop, as a GeoJSON FeatureCollection.

    Each stop carries the ids of the routes that serve it, so the map can dim
    the stops off the route a user has selected without a second request.
    """
    rows = db.execute(
        select(Stop.id, Stop.name, func.ST_X(Stop.geom), func.ST_Y(Stop.geom)).order_by(Stop.id)
    ).all()

    # One extra query for the whole join table rather than one per stop. The
    # table has a row per (route, stop) pair — a few hundred for a city.
    links = db.execute(
        select(RouteStop.stop_id, RouteStop.route_id).order_by(RouteStop.stop_id, RouteStop.seq)
    ).all()
    serving: dict[int, list[int]] = {}
    for stop_id, route_id in links:
        serving.setdefault(stop_id, []).append(route_id)

    features = [
        {
            "type": "Feature",
            "id": stop_id,
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {
                "id": stop_id,
                "name": name,
                "route_ids": serving.get(stop_id, []),
            },
        }
        for stop_id, name, lon, lat in rows
    ]
    return {"type": "FeatureCollection", "features": features}


def _route_feature(
    row: Any, *, include_geometry: bool, stops: list[dict[str, Any]] | None
) -> dict[str, Any]:
    """Shape one selected route row into a GeoJSON Feature.

    `ST_AsGeoJSON` returns the geometry already encoded, so it is parsed rather
    than rebuilt — re-serialising coordinates in Python would be slower and
    would round them differently.
    """
    route_id, short_name, long_name, geojson, length_m, stop_count, trace_count = row

    properties: dict[str, Any] = {
        "id": route_id,
        "short_name": short_name,
        "long_name": long_name,
        # Rounded to the metre: the underlying line is a cluster medoid
        # simplified to a tolerance far coarser than that, so the extra digits
        # would be false precision.
        "length_m": round(length_m) if length_m is not None else None,
        "stop_count": stop_count,
        "trace_count": trace_count,
    }
    if stops is not None:
        properties["stops"] = stops

    return {
        "type": "Feature",
        "id": route_id,
        # A route with no geometry should not happen — clustering always writes
        # one — but the column is nullable, and a null geometry is valid GeoJSON
        # that MapLibre skips, where a malformed one would break the layer.
        "geometry": json.loads(geojson) if (include_geometry and geojson) else None,
        "properties": properties,
    }
