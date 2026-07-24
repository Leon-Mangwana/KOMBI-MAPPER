from datetime import datetime

from pydantic import BaseModel, Field


class TracePointIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    recorded_at: datetime | None = None
    accuracy_m: float | None = None
    speed_mps: float | None = None


class TraceIn(BaseModel):
    contributor: str = Field(min_length=1, max_length=255)
    points: list[TracePointIn]


class TraceCreated(BaseModel):
    id: int
    contributor: str
    status: str
    point_count: int
