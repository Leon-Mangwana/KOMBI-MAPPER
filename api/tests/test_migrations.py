"""The Alembic migrations and the ORM models must describe the same schema.

Every other test builds its tables with `metadata.create_all`, which is fast
but proves nothing about `alembic/versions/`. Production never calls
`create_all`: the Dockerfile runs `alembic upgrade head` on boot, so a column
added to `models.py` without a migration works in every test and 500s on
Render. This test runs the migrations for real and asks Alembic's autogenerate
whether it would emit anything on top of them. The answer must be nothing.

It borrows the session engine's database, rebuilds it through Alembic, and
hands it back the way `conftest.engine` left it, so test order does not matter.
"""

import os
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import text

from alembic import command
from app.config import settings
from app.database import Base

API_DIR = Path(__file__).resolve().parents[1]

# The same default as conftest.py; it is not importable from there without
# making tests/ a package.
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@localhost:5432/kombi_test",
)

#: Tables Alembic should see but the models do not own: PostGIS's own, and the
#: migration bookkeeping. Anything else unexpected is a finding, not noise.
_FOREIGN_TABLES = {"spatial_ref_sys", "alembic_version"}


def _alembic_config() -> Config:
    config = Config(str(API_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(API_DIR / "alembic"))
    return config


def _include_object(obj, name, type_, reflected, compare_to) -> bool:
    if type_ == "table" and name in _FOREIGN_TABLES:
        return False
    return True


@pytest.fixture()
def migrated(engine, monkeypatch):
    """A database at `alembic upgrade head`, restored to `create_all` afterwards."""
    # env.py reads the URL from settings, not alembic.ini — point it at the
    # disposable test database for the duration.
    monkeypatch.setattr(settings, "database_url", TEST_DATABASE_URL)

    Base.metadata.drop_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))

    command.upgrade(_alembic_config(), "head")
    yield engine

    Base.metadata.drop_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
    Base.metadata.create_all(engine)


def test_migrations_match_the_models(migrated):
    with migrated.connect() as conn:
        context = MigrationContext.configure(
            conn,
            opts={"include_object": _include_object, "compare_type": True},
        )
        diff = compare_metadata(context, Base.metadata)

    assert diff == [], "alembic autogenerate would emit:\n" + "\n".join(repr(d) for d in diff)


def test_migrations_downgrade_cleanly(migrated, monkeypatch):
    """`downgrade base` must undo everything `upgrade head` did, or a failed
    deploy cannot be rolled back."""
    command.downgrade(_alembic_config(), "base")

    with migrated.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
        }

    assert tables - _FOREIGN_TABLES == set()
