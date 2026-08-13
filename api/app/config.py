from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

#: Where `pipeline/gtfs.py` writes by default, resolved from this file rather
#: than the working directory so that `uvicorn app.main:app` finds it from
#: anywhere in the monorepo. Deployments where the two run in separate
#: containers must set GTFS_PATH to whatever volume they share.
_DEFAULT_GTFS_PATH = Path(__file__).resolve().parents[2] / "pipeline" / "gtfs_out" / "gtfs.zip"


class Settings(BaseSettings):
    """Application configuration, populated from the environment / .env file."""

    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/kombi"

    # The GTFS feed the pipeline exported, served at GET /gtfs.zip. The API
    # never builds it — see app/routers/gtfs.py.
    gtfs_path: Path = _DEFAULT_GTFS_PATH

    # Browser origins allowed to call the API. Both front ends use fetch(), so
    # their origins must be listed here or the browser will block the requests.
    # Defaults cover the Vite dev server and `vite preview` for each: capture on
    # 5173/4173, web on 5174/4174. The two ports per app are pinned in their
    # vite.config.ts so this list can name them.
    #
    # Comma-separated rather than a list field: pydantic-settings expects JSON
    # for complex types, which is awkward to write in a .env file.
    cors_origins: str = (
        "http://localhost:5173,http://localhost:4173,http://localhost:5174,http://localhost:4174"
    )

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


settings = Settings()
