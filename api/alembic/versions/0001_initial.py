"""initial schema: routes, stops, traces, trace_points, route_stops

Revision ID: 0001_initial
Revises:
Create Date: 2026-07-24

"""

from collections.abc import Sequence

import geoalchemy2
import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")

    op.create_table(
        "routes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("short_name", sa.String(length=255), nullable=True),
        sa.Column("long_name", sa.String(length=255), nullable=True),
        sa.Column(
            "geom",
            geoalchemy2.Geometry(geometry_type="LINESTRING", srid=4326, spatial_index=False),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_routes_geom", "routes", ["geom"], postgresql_using="gist")

    op.create_table(
        "stops",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column(
            "geom",
            geoalchemy2.Geometry(geometry_type="POINT", srid=4326, spatial_index=False),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_stops_geom", "stops", ["geom"], postgresql_using="gist")

    op.create_table(
        "traces",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("contributor", sa.String(length=255), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="uploaded",
            nullable=False,
        ),
        sa.Column("route_id", sa.Integer(), nullable=True),
        sa.Column(
            "matched_geom",
            geoalchemy2.Geometry(geometry_type="LINESTRING", srid=4326, spatial_index=False),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["route_id"], ["routes.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_traces_route_id", "traces", ["route_id"])
    op.create_index("ix_traces_matched_geom", "traces", ["matched_geom"], postgresql_using="gist")

    op.create_table(
        "trace_points",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("trace_id", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column(
            "geom",
            geoalchemy2.Geometry(geometry_type="POINT", srid=4326, spatial_index=False),
            nullable=False,
        ),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accuracy_m", sa.Float(), nullable=True),
        sa.Column("speed_mps", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["trace_id"], ["traces.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("trace_id", "seq", name="uq_trace_points_trace_seq"),
    )
    op.create_index("ix_trace_points_trace_id", "trace_points", ["trace_id"])
    op.create_index("ix_trace_points_geom", "trace_points", ["geom"], postgresql_using="gist")

    op.create_table(
        "route_stops",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("route_id", sa.Integer(), nullable=False),
        sa.Column("stop_id", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["route_id"], ["routes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["stop_id"], ["stops.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("route_id", "seq", name="uq_route_stops_route_seq"),
    )
    op.create_index("ix_route_stops_route_id", "route_stops", ["route_id"])
    op.create_index("ix_route_stops_stop_id", "route_stops", ["stop_id"])


def downgrade() -> None:
    op.drop_table("route_stops")
    op.drop_index("ix_trace_points_geom", table_name="trace_points")
    op.drop_index("ix_trace_points_trace_id", table_name="trace_points")
    op.drop_table("trace_points")
    op.drop_index("ix_traces_matched_geom", table_name="traces")
    op.drop_index("ix_traces_route_id", table_name="traces")
    op.drop_table("traces")
    op.drop_index("ix_stops_geom", table_name="stops")
    op.drop_table("stops")
    op.drop_index("ix_routes_geom", table_name="routes")
    op.drop_table("routes")
