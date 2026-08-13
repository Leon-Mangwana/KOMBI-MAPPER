"""Pipeline configuration and the shared database engine.

Mirrors `api/app/config.py` — the pipeline reads the same database the API
writes to, but deliberately does not import the API package: the two are
separate installables and the pipeline only needs the `trace_points` table.
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import Engine, create_engine


class Settings(BaseSettings):
    """Pipeline configuration, populated from the environment / .env file."""

    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/kombi"

    #: Base URL of the OSRM backend used for map matching. Matches the `osrm`
    #: service in ../infra/docker-compose.yml.
    osrm_url: str = "http://localhost:5000"

    #: Where `gtfs.py` writes the feed. The API serves this file at
    #: `GET /gtfs.zip`, so a deployment must point both at the same path —
    #: `GTFS_PATH` on the API side.
    gtfs_path: Path = Path("gtfs_out/gtfs.zip")

    #: `agency.txt`. There is no agency behind an informal network, so the feed
    #: publisher is the agency — see `gtfs.Agency`. A real deployment must set
    #: at least the name and URL; the defaults are placeholders that are valid
    #: GTFS rather than anything a passenger should be shown.
    gtfs_agency_id: str = "kombi"
    gtfs_agency_name: str = "Kombi Route Mapper"
    gtfs_agency_url: str = "https://kombi.example.org"
    gtfs_agency_lang: str = "en"

    #: IANA timezone the feed's clock times are in. Also the timezone the
    #: `gtfs.BANDS` time bands are read in — a morning peak is a local fact.
    gtfs_agency_timezone: str = "Africa/Harare"

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
