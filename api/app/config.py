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

    # Shared secret for the operator-only endpoints, sent as `X-Admin-Token`.
    #
    # Empty by default, and an empty token does not mean "no authentication
    # required" — it means those endpoints are unavailable. Failing closed
    # matters here because the endpoint it guards returns one contributor's
    # complete timestamped movement history, and a deployment that forgot to
    # set this would otherwise publish it.
    admin_token: str = ""

    # --- Ingest limits -----------------------------------------------------
    #
    # `POST /traces` is open by design: contribution is the point, and requiring
    # an account would cost more traces than it saved. Open does not mean
    # unbounded, so the limits below are what stands between the database and
    # anybody with curl.

    # Points one trace may contain. 20 000 is about five and a half hours at the
    # 1 Hz the capture PWA records at — far beyond any real kombi ride, while
    # still rejecting a payload built to exhaust memory.
    max_trace_points: int = 20_000

    # Largest body accepted on an upload. A full-length trace is roughly 2.5 MB
    # of JSON, so this leaves generous headroom and still refuses a body that
    # would be parsed before any field limit could apply.
    max_upload_bytes: int = 8_000_000

    # Uploads allowed per client per window. A contributor finishing a ride
    # sends one trace; the queue may flush a few at once after a spell offline,
    # which is what the per-minute allowance is sized for.
    uploads_per_minute: int = 6
    uploads_per_hour: int = 40

    # Read the client address from `X-Forwarded-For`. Required behind a
    # platform proxy (Render, Fly, a CDN) — without it every request appears to
    # come from the proxy and one abuser rate-limits everybody. Turn it off when
    # the API is exposed directly, where the header is attacker-controlled.
    trust_proxy_headers: bool = True

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


settings = Settings()
