"""SQLAlchemy 2.0 ORM models.

All geometry columns are stored as EPSG:4326 (WGS 84). Any metric calculation
must reproject to EPSG:32736 (UTM 36S) — never compute distances in degrees.
See CLAUDE.md.

Spatial indexes are declared explicitly via ``__table_args__`` with
``postgresql_using="gist"`` (and ``spatial_index=False`` on the columns) so that
both ``metadata.create_all`` and the Alembic migration produce the same DDL.
"""

from __future__ import annotations

from datetime import datetime

from geoalchemy2 import Geometry
from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class Route(Base):
    __tablename__ = "routes"

    id: Mapped[int] = mapped_column(primary_key=True)
    short_name: Mapped[str | None] = mapped_column(String(255))
    long_name: Mapped[str | None] = mapped_column(String(255))
    geom = mapped_column(Geometry("LINESTRING", srid=4326, spatial_index=False))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    traces: Mapped[list[Trace]] = relationship(back_populates="route")
    route_stops: Mapped[list[RouteStop]] = relationship(
        back_populates="route",
        cascade="all, delete-orphan",
        order_by="RouteStop.seq",
    )

    __table_args__ = (Index("ix_routes_geom", "geom", postgresql_using="gist"),)


class Stop(Base):
    __tablename__ = "stops"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str | None] = mapped_column(String(255))
    geom = mapped_column(Geometry("POINT", srid=4326, spatial_index=False), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    route_stops: Mapped[list[RouteStop]] = relationship(back_populates="stop")

    __table_args__ = (Index("ix_stops_geom", "geom", postgresql_using="gist"),)


class Trace(Base):
    __tablename__ = "traces"

    id: Mapped[int] = mapped_column(primary_key=True)
    contributor: Mapped[str] = mapped_column(String(255), nullable=False)
    recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="uploaded", server_default="uploaded"
    )
    route_id: Mapped[int | None] = mapped_column(
        ForeignKey("routes.id", ondelete="SET NULL"), index=True
    )
    matched_geom = mapped_column(Geometry("LINESTRING", srid=4326, spatial_index=False))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    route: Mapped[Route | None] = relationship(back_populates="traces")
    points: Mapped[list[TracePoint]] = relationship(
        back_populates="trace",
        cascade="all, delete-orphan",
        order_by="TracePoint.seq",
    )

    __table_args__ = (
        Index("ix_traces_matched_geom", "matched_geom", postgresql_using="gist"),
    )


class TracePoint(Base):
    __tablename__ = "trace_points"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    trace_id: Mapped[int] = mapped_column(
        ForeignKey("traces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    geom = mapped_column(Geometry("POINT", srid=4326, spatial_index=False), nullable=False)
    recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accuracy_m: Mapped[float | None] = mapped_column(Float)
    speed_mps: Mapped[float | None] = mapped_column(Float)

    trace: Mapped[Trace] = relationship(back_populates="points")

    __table_args__ = (
        UniqueConstraint("trace_id", "seq", name="uq_trace_points_trace_seq"),
        Index("ix_trace_points_geom", "geom", postgresql_using="gist"),
    )


class RouteStop(Base):
    __tablename__ = "route_stops"

    id: Mapped[int] = mapped_column(primary_key=True)
    route_id: Mapped[int] = mapped_column(
        ForeignKey("routes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stop_id: Mapped[int] = mapped_column(
        ForeignKey("stops.id", ondelete="CASCADE"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)

    route: Mapped[Route] = relationship(back_populates="route_stops")
    stop: Mapped[Stop] = relationship(back_populates="route_stops")

    __table_args__ = (
        UniqueConstraint("route_id", "seq", name="uq_route_stops_route_seq"),
    )
