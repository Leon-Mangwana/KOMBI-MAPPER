from datetime import datetime

from pydantic import BaseModel, Field

from .config import settings


class TracePointIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    recorded_at: datetime | None = None
    accuracy_m: float | None = None
    speed_mps: float | None = None


class TraceIn(BaseModel):
    contributor: str = Field(min_length=1, max_length=255)
    # Bounded at both ends. A trace with no points is nothing to store, and one
    # with more than `MAX_TRACE_POINTS` is longer than any real ride — see
    # config.Settings. The size limit in security.py is the coarser guard that
    # runs before this one, since this cannot apply until the body is parsed.
    points: list[TracePointIn] = Field(min_length=1, max_length=settings.max_trace_points)


class TraceCreated(BaseModel):
    id: int
    contributor: str
    status: str
    point_count: int
