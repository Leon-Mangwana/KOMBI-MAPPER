from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration, populated from the environment / .env file."""

    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/kombi"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
