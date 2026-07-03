"""Shared settings for backend + MCP services. Loaded from env / `.env`."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env", "../../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Database ---
    # Async driver (asyncpg). Host is `db` in Compose, `localhost` from the host.
    database_url: str = "postgresql+asyncpg://jobpilot:jobpilot_dev_pw@localhost:5432/jobpilot"

    # --- Redis ---
    redis_url: str = "redis://localhost:6379/0"

    # --- Auth / JWT (user-facing access tokens) ---
    jwt_secret: str = "dev-insecure-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60

    # --- Seeded test user (created by the seed script, not a migration) ---
    test_user_email: str = "test@jobpilot.dev"
    test_user_password: str = "changeme123"

    # --- CORS ---
    frontend_origin: str = "http://localhost:3000"

    # --- MCP request context signing (backend -> MCP service tokens) ---
    # Separate secret so MCP context tokens can be rotated independently of login
    # tokens. Defaults to jwt_secret for zero-config dev.
    mcp_shared_secret: str = ""
    mcp_context_ttl_seconds: int = 120

    # --- MCP service endpoints (used by the backend MCP clients) ---
    db_mcp_url: str = "http://localhost:8001/mcp"
    resume_mcp_url: str = "http://localhost:8002/mcp"

    # --- Storage (S3 / MinIO) ---
    storage_backend: str = "s3"  # "s3" | "local"
    storage_bucket: str = "jobpilot-resumes"
    s3_endpoint_url: str = "http://localhost:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_region: str = "us-east-1"
    storage_local_dir: str = "./.storage"

    # --- Resume upload guardrails ---
    resume_max_bytes: int = 10 * 1024 * 1024  # 10 MB
    resume_retention_days: int = 365

    # --- Embeddings (columns defined now; population deferred to Phase 3) ---
    embedding_dim: int = 1024

    def effective_mcp_secret(self) -> str:
        return self.mcp_shared_secret or self.jwt_secret


@lru_cache
def get_settings() -> Settings:
    return Settings()
