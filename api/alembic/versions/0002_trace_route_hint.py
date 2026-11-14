"""traces.route_hint: what the rider said they rode

Revision ID: 0002_trace_route_hint
Revises: 0001_initial
Create Date: 2026-11-13

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_trace_route_hint"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable with no default: every existing trace simply has no hint, and
    # adding the column is a metadata-only change on Postgres.
    op.add_column("traces", sa.Column("route_hint", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("traces", "route_hint")
