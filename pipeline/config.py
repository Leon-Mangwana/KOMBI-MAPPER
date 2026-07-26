"""Pipeline configuration and the shared database engine.

Mirrors `api/app/config.py` — the pipeline reads the same database the API
writes to, but deliberately does not import the API package: the two are
separate installables and the pipeline only needs the `trace_points` table.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import Engine, create_engine


class Settings(BaseSettings):
    """Pipeline configuration, populated from the environment / .env file."""

    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/kombi"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()

_engine: Engine | None = None


def get_engine() -> Engine:
    """Return the process-wide engine, creating it on first use.

    Creation is lazy so that importing `clean` does not require a reachable
    database — the tests exercise the cleaning functions on synthetic frames.
    """
    global _engine
    if _engine is None:
        _engine = create_engine(settings.database_url, future=True, pool_pre_ping=True)
    return _engine
