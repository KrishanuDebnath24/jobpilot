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
    config_mcp_url: str = "http://localhost:8003/mcp"
    sources_mcp_url: str = "http://localhost:8004/mcp"

    # --- Anthropic (Phase 2+). Required in services that call an LLM
    # (sources-mcp for crawl extraction). Fail-fast via require_anthropic_key(). ---
    anthropic_api_key: str = ""
    # Per-agent model config: a config value, never a constant, so tiers can be
    # promoted without code changes. Scout only needs the cheap extraction tier.
    scout_extractor_model: str = "claude-haiku-4-5-20251001"
    # Hard per-call output cap — a code-level spend guard alongside the Console cap.
    llm_max_output_tokens: int = 1024

    # --- Adzuna connector (free developer tier). Backend/sources-mcp env only. ---
    adzuna_app_id: str = ""
    adzuna_app_key: str = ""
    # Adzuna's API does NOT support the Republic of Ireland ("ie" -> UNSUPPORTED_COUNTRY).
    # It DOES cover Northern Ireland (Belfast/Derry) under "gb", so that's the default;
    # RoI (Dublin) coverage comes from the Greenhouse + crawler connectors. Config-driven
    # so "ie" activates automatically if Adzuna ever adds it. See DECISIONS.md (Phase 2).
    adzuna_country: str = "gb"
    adzuna_results_per_page: int = 50  # Adzuna max page size
    adzuna_max_locations: int = 3  # bound free-tier quota: one call per location, capped

    # --- Crawler connector politeness + spend guards ---
    # The board rejects unknown UAs at the network layer (disconnects non-browsers),
    # so we send a mainstream browser UA. Politeness is still enforced the ways that
    # matter: we honor robots.txt (the site's declared crawl policy), a per-domain
    # rate limit, and a hard page cap. See DECISIONS.md (Phase 2, crawler).
    crawler_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
    crawler_board: str = "irishjobs"  # single Irish board (no public API)
    crawler_max_pages: int = 3  # cap pages fetched per run -> caps extraction calls
    crawler_min_delay_seconds: float = 2.0  # per-domain minimum delay between requests
    crawler_timeout_seconds: float = 20.0
    crawler_max_text_chars: int = 12000  # cap page text sent to the LLM (spend guard)

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

    def require_anthropic_key(self) -> str:
        """Fail-fast accessor for the Anthropic key. Services that make LLM calls
        call this at boot so a missing/placeholder key is a clear, immediate error
        rather than a confusing 401 on the first real request."""
        key = (self.anthropic_api_key or "").strip()
        if not key or not key.startswith("sk-ant-"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY is missing or looks like a placeholder. Set a real "
                "key (starts with 'sk-ant-') in the environment before starting a "
                "service that makes LLM calls (Phase 2+)."
            )
        return key


@lru_cache
def get_settings() -> Settings:
    return Settings()
