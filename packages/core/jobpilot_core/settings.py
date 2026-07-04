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
    # Cap jobs scored per Qualifier run (bounds LLM spend on the gated refine pass).
    qualify_max_jobs: int = 200

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

    # --- Aggregator connectors for Republic-of-Ireland coverage (Adzuna can't serve
    #     RoI). Backend/sources-mcp env only. Jooble + Careerjet are free (on by
    #     default); JSearch is RapidAPI-metered (off by default, page-capped). ---
    jooble_api_key: str = ""
    careerjet_api_key: str = ""  # Careerjet affiliate id (affid)
    careerjet_locale: str = "en_IE"  # the Ireland site
    careerjet_user_ip: str = "127.0.0.1"  # representative server IP for attribution
    # JSearch (Google-for-Jobs) has two provider options; the connector prefers
    # OpenWeb Ninja (its canonical source) when JSEARCH_API_KEY is set, else RapidAPI.
    jsearch_api_key: str = ""  # OpenWeb Ninja (X-API-Key header)
    jsearch_base_url: str = "https://api.openwebninja.com/jsearch/search-v2"
    rapidapi_key: str = ""  # JSearch via RapidAPI (fallback)
    jsearch_host: str = "jsearch.p.rapidapi.com"
    jsearch_country: str = "ie"
    jsearch_max_pages: int = 1  # hard cost cap on the metered source
    jsearch_date_posted: str = "all"
    aggregator_results_per_page: int = 50  # cap per aggregator call

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

    def require_jooble_key(self) -> str:
        key = (self.jooble_api_key or "").strip()
        if not key:
            raise RuntimeError("JOOBLE_API_KEY is not configured (get one free at jooble.org/api/about)")
        return key

    def require_careerjet_affid(self) -> str:
        key = (self.careerjet_api_key or "").strip()
        if not key:
            raise RuntimeError("CAREERJET_API_KEY (affiliate id) is not configured (careerjet.com/partners)")
        return key

    def jsearch_provider(self) -> tuple[str, str, dict]:
        """Pick the JSearch provider and return (name, url, headers). Prefers
        OpenWeb Ninja (canonical JSearch source) when its key is set, else RapidAPI.
        Raises a clear error if neither is configured."""
        own = (self.jsearch_api_key or "").strip()
        if own:
            return ("openwebninja", self.jsearch_base_url, {"X-API-Key": own})
        rapid = (self.rapidapi_key or "").strip()
        if rapid:
            return (
                "rapidapi",
                "https://jsearch.p.rapidapi.com/search",
                {"X-RapidAPI-Key": rapid, "X-RapidAPI-Host": self.jsearch_host},
            )
        raise RuntimeError(
            "No JSearch key configured (set JSEARCH_API_KEY for OpenWeb Ninja, "
            "or RAPIDAPI_KEY with a JSearch subscription)"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
