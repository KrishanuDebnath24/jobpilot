"""Application settings, loaded from environment / `.env`."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),  # backend/.env or repo-root .env
        env_file_encoding="utf-8",
        extra="ignore",  # ignore vars meant for other services (Postgres, Next, ...)
    )

    # App
    app_name: str = "jobpilot-backend"
    version: str = "0.1.0"

    # Auth / JWT
    jwt_secret: str = "dev-insecure-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60

    # Phase 0 seeded test user (moves to the DB in Phase 1)
    test_user_email: str = "test@jobpilot.dev"
    test_user_password: str = "changeme123"

    # CORS
    frontend_origin: str = "http://localhost:3000"


@lru_cache
def get_settings() -> Settings:
    """Cached singleton so env is parsed once per process."""
    return Settings()
