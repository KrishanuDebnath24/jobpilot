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
    pipeline_mcp_url: str = "http://localhost:8005/mcp"

    # --- Orchestrator (Phase 4). LangGraph = control plane (own Celery queue so it
    # never blocks a stage); stages run on the default queue = data plane. ---
    orchestrator_queue: str = "orchestrator"
    data_queue: str = "celery"  # Celery's default queue (the existing worker)
    stage_timeout_seconds: int = 1800  # max wait for one stage to finish
    pipeline_stale_seconds: int = 3600  # reap an umbrella stuck 'running' past this
    beat_tick_seconds: int = 60  # how often the scheduler checks for due pipelines

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

    # --- Phase 5 (Analyzer + Enhancer). Two-model split, config-driven so tiers can
    #     be promoted without code changes. Analyzer = high-volume extraction (Haiku,
    #     batched + cached); Enhancer = lower-volume user-facing reasoning (Sonnet). ---
    analyzer_model: str = "claude-haiku-4-5-20251001"
    enhancer_model: str = "claude-sonnet-5"
    # The Enhancer emits three buckets (skills/projects/bullet_rewrites) — it needs
    # more room than the extraction default so its output isn't truncated mid-bucket.
    enhancer_max_tokens: int = 3000
    analyze_max_jobs: int = 200  # hard cap on jobs analyzed per run (spend guard)
    # Selected/single mode runs synchronous cached calls; at/above this count a run
    # switches to the Batch API (50% cheaper, non-interactive).
    analyze_batch_threshold: int = 8
    batch_poll_interval_seconds: float = 10.0  # how often the batch node polls status
    batch_max_wait_seconds: int = 1800  # give up (gracefully) after this
    analyze_max_desc_chars: int = 6000  # cap job description sent to the LLM (spend guard)
    # Append Analyze→Enhance to the autonomous Scout→Qualify pipeline? Off by default:
    # analyzing every scheduled scout is expensive and presumptuous (Phase 5 decision).
    analyze_in_pipeline: bool = False

    # --- Phase 6 (Concierge). A Sonnet tier: reasoning/routing/judgment over a scoped
    #     toolset. Interactive (no batch); the stable system prompt + tool defs cache.
    #     Config-driven so the tier can be promoted without code changes. ---
    concierge_model: str = "claude-sonnet-5"
    concierge_max_tokens: int = 2048
    # Hard cap on tool-call rounds per chat turn (bounds an agent that loops on tools).
    concierge_max_tool_iterations: int = 8

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

    # --- Embeddings (Phase 7). Local sentence-transformers by default — zero API cost
    #     (the heavy dep is torch), pluggable via EMBEDDING_BACKEND. The DB pgvector
    #     columns are fixed at 1024 dims (Phase 1); a model with a smaller native dim is
    #     zero-padded to fit, which preserves cosine ordering. A dependency-free hashing
    #     backend (bag-of-words) is used for tests / offline and as a graceful fallback. ---
    embedding_dim: int = 1024
    embedding_backend: str = "sentence_transformers"  # sentence_transformers | hashing
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"  # 384-dim, ~80MB
    embedding_batch_size: int = 64  # texts per encode call (bounds memory)
    embedding_max_chars: int = 4000  # cap text length fed to the embedder (per row)

    # --- Phase 7 (Query agent). Read-only Q&A over the user's own data + work status.
    #     Haiku by default — routing + synthesizing retrieved results is mechanical;
    #     config-driven so the tier can be promoted. Interactive (no batch); the stable
    #     system prompt + tool defs cache. ---
    query_model: str = "claude-haiku-4-5"
    query_max_tokens: int = 1024
    query_max_tool_iterations: int = 6
    query_semantic_limit: int = 8  # rows returned by a semantic similarity search

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
