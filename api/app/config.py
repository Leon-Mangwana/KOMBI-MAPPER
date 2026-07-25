from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration, populated from the environment / .env file."""

    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/kombi"

    # Browser origins allowed to call the API. The capture PWA uploads traces
    # with fetch(), so its origin must be listed here or the browser will block
    # the request. Defaults cover the Vite dev server and `vite preview`.
    #
    # Comma-separated rather than a list field: pydantic-settings expects JSON
    # for complex types, which is awkward to write in a .env file.
    cors_origins: str = "http://localhost:5173,http://localhost:4173"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


settings = Settings()
