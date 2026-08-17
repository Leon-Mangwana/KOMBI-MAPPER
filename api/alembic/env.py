from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

import app.models  # noqa: F401  -- imported for the side effect of populating metadata
from alembic import context
from app.config import settings
from app.database import Base

config = context.config

# `%` is doubled because Alembic keeps its options in a ConfigParser, where a
# lone `%` starts an interpolation token and raises before anything connects.
#
# This is not a hypothetical: a database password containing any character that
# has to be percent-encoded in a URL — `@` becomes `%40`, `/` becomes `%2F` —
# arrives here already encoded, and hosted Postgres providers generate exactly
# such passwords. Doubling is the escape ConfigParser itself defines, and it is
# undone before the value reaches SQLAlchemy.
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
