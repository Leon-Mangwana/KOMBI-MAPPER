from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from geoalchemy2.elements import WKTElement
from sqlalchemy import func, insert, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Trace, TracePoint
from ..schemas import TraceCreated, TraceIn
from ..security import require_admin

router = APIRouter(prefix="/traces", tags=["traces"])


@router.post("", status_code=status.HTTP_201_CREATED, response_model=TraceCreated)
def create_trace(payload: TraceIn, db: Session = Depends(get_db)) -> TraceCreated:
    """Bulk-insert a trace and all its points in a single transaction.

    Open to anyone: contribution is the point of the project, and an account
    requirement would cost more traces than it saved. The bounds on what may be
    sent are on `TraceIn` and in the middleware — see app/security.py.
    """
    recorded_ats = [p.recorded_at for p in payload.points if p.recorded_at is not None]
    trace = Trace(
        contributor=payload.contributor,
        status="uploaded",
        recorded_at=min(recorded_ats) if recorded_ats else None,
    )
    db.add(trace)
    db.flush()  # assigns trace.id without committing

    db.execute(
        insert(TracePoint),
        [
            {
                "trace_id": trace.id,
                "seq": seq,
                "geom": WKTElement(f"POINT({p.lon} {p.lat})", srid=4326),
                "recorded_at": p.recorded_at,
                "accuracy_m": p.accuracy_m,
                "speed_mps": p.speed_mps,
            }
            for seq, p in enumerate(payload.points)
        ],
    )
    db.commit()

    return TraceCreated(
        id=trace.id,
        contributor=trace.contributor,
        status=trace.status,
        point_count=len(payload.points),
    )


@router.get("/{trace_id}", dependencies=[Depends(require_admin)])
def get_trace(trace_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Return a trace as a GeoJSON Feature (LineString of its ordered points).

    **Operator-only.** This is one contributor's raw ride: their name, every
    position they passed through, and the time they were there. Trace ids are
    sequential, so an open version of this endpoint could be walked from 1
    upwards to reconstruct where individual people travel. The public view of
    the data is `/routes` and `/stops`, which are aggregates over many riders.

    Requires `X-Admin-Token`; see app/security.py.
    """
    trace = db.get(Trace, trace_id)
    if trace is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trace not found.")

    rows = db.execute(
        select(
            TracePoint.seq,
            func.ST_X(TracePoint.geom),
            func.ST_Y(TracePoint.geom),
            TracePoint.recorded_at,
            TracePoint.accuracy_m,
            TracePoint.speed_mps,
        )
        .where(TracePoint.trace_id == trace_id)
        .order_by(TracePoint.seq)
    ).all()

    coordinates = [[lon, lat] for _seq, lon, lat, *_rest in rows]
    if len(coordinates) >= 2:
        geometry: dict[str, Any] | None = {"type": "LineString", "coordinates": coordinates}
    elif coordinates:
        geometry = {"type": "Point", "coordinates": coordinates[0]}
    else:
        geometry = None

    return {
        "type": "Feature",
        "geometry": geometry,
        "properties": {
            "id": trace.id,
            "contributor": trace.contributor,
            "status": trace.status,
            "route_id": trace.route_id,
            "recorded_at": trace.recorded_at.isoformat() if trace.recorded_at else None,
            "points": [
                {
                    "seq": seq,
                    "recorded_at": recorded_at.isoformat() if recorded_at else None,
                    "accuracy_m": accuracy_m,
                    "speed_mps": speed_mps,
                }
                for seq, _lon, _lat, recorded_at, accuracy_m, speed_mps in rows
            ],
        },
    }
