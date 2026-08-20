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
    # The Qualifier's fit-refine pass (gate-passers only). A Sonnet tier: it reads the FULL
    # job + resume, so it grades skill coverage without the deterministic taxonomy's blind
    # spots — required skills the taxonomy doesn't know are still detected.
    qualifier_refine_model: str = "claude-sonnet-5"
    qualifier_refine_max_tokens: int = 1500  # room for matched/missing skills + reasons

    # --- Resume structuring (resume-mcp). A STRONG tier: the model turns extracted resume
    #     text into the typed StructuredResume shape (no rules-based parsing), so accuracy
    #     matters more than cost — this runs once per upload, not per job. ---
    resume_parser_model: str = "claude-opus-4-8"
    resume_parser_max_tokens: int = 4000  # room for a full structured resume (skills + roles)

    # --- Phase 5 (Analyzer + Enhancer). Two-model split, config-driven so tiers can
    #     be promoted without code changes. Analyzer = high-volume extraction (Haiku,
    #     batched + cached); Enhancer = lower-volume user-facing reasoning (Sonnet). ---
    analyzer_model: str = "claude-haiku-4-5-20251001"
    enhancer_model: str = "claude-sonnet-5"
    # The Enhancer emits three buckets (skills/projects/bullet_rewrites) — it needs
    # more room than the extraction default so its output isn't truncated mid-bucket.
    enhancer_max_tokens: int = 3000
    # The Gap-closer (same Sonnet tier as the Enhancer) emits three buckets where every
    # item also carries an interview talking point, so it needs more room than the
    # Enhancer. Bounded per run by gap_close_max_jobs.
    gap_closer_max_tokens: int = 4000
    gap_close_max_jobs: int = 20  # bound a gap-close selection (spend guard)
    analyze_max_jobs: int = 200  # hard cap on jobs analyzed per run (spend guard)
    # Selected/single mode runs synchronous cached calls; at/above this count a run
    # switches to the Batch API (50% cheaper, non-interactive).
    # 0 disables the Batch API entirely: analyze always runs synchronously. The batch is
    # half price but asynchronous, and it was costing ~40 minutes of wall clock to save
    # ~5c on a 20-job run — a bad trade for a run someone is sitting and watching. Set a
    # positive threshold to re-enable batching at that job count.
    analyze_batch_threshold: int = 0
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

    # --- Phase 6.5 (CV alignment loop). Two agents alternate per selected job: the
    #     Rewriter reshapes the CV toward the job, the Critic grades the match and says
    #     what to fix. Both are a Sonnet tier (user-facing document quality) and share
    #     one cached prefix (resume + rules + locked snapshot) across every round and
    #     every job in a selection. Config-driven so tiers can be promoted. ---
    rewriter_model: str = "claude-sonnet-5"
    cv_critic_model: str = "claude-sonnet-5"
    rewriter_max_tokens: int = 4000  # a full structured CV + skill evidence + notes
    cv_critic_max_tokens: int = 3000  # a verdict + scored dimensions + fixes (a bad draft
    #                                   draws a long issue list — observed ~1200 output)
    # Loop exit: the Critic must return verdict='aligned' with a score at or above this
    # bar and no integrity flag. Otherwise it rewrites and tries again, up to the cap.
    # Raising the threshold buys alignment with more rounds — i.e. linearly more spend.
    cv_alignment_threshold: float = 0.85
    # Rewrite→critique rounds per job, and the dominant cost lever on this path: a job
    # costs 1 + 2*rounds Sonnet calls (one rewrite, then a critique + a re-rewrite per
    # round that fails the bar).
    #   0 = SINGLE PASS (the default): one Rewriter call, no Critic, no revision. The
    #       deterministic gate still runs — locked facts are re-imposed and un-evidenced
    #       skills are dropped — so the draft is exactly as honest, just ungraded. The
    #       draft lands unscored for the human to review, which is what the review-and-
    #       approve step in the UI is for.
    #   n>0 = restore the loop with a hard cap of n rounds (4 was the previous default).
    cv_max_rounds: int = 0
    cv_tailor_max_jobs: int = 20  # bound a selection (spend guard)

    # --- Adzuna / Jooble / Careerjet were removed (2026-08-07). All three returned
    #     snippet teasers rather than full postings, and the stage-2 resolver measured
    #     ZERO applyable jobs behind any of them: their apply links are tracking
    #     redirects, and 0 of 398 contained an ATS domain. Adzuna additionally could not
    #     serve the Republic of Ireland at all (its API rejects "ie") and was configured
    #     for "gb", so it was importing Great Britain and Northern Ireland into an
    #     Ireland-only pool. RoI coverage now comes from Workable + Greenhouse, both
    #     full-text. See DECISIONS.md. ---

    # --- JSearch (Google-for-Jobs). Metered, OFF by default, page-capped. Kept because
    #     unlike the removed aggregators it returns full descriptions. Two provider
    #     options; prefers OpenWeb Ninja (canonical) when JSEARCH_API_KEY is set. ---
    jsearch_api_key: str = ""  # OpenWeb Ninja (X-API-Key header)
    jsearch_base_url: str = "https://api.openwebninja.com/jsearch/search-v2"
    rapidapi_key: str = ""  # JSearch via RapidAPI (fallback)
    jsearch_host: str = "jsearch.p.rapidapi.com"
    jsearch_country: str = "ie"
    jsearch_max_pages: int = 1  # hard cost cap on the metered source
    jsearch_date_posted: str = "all"
    aggregator_results_per_page: int = 50  # cap per aggregator call

    # --- The Muse (free public jobs API; a key is optional and only raises rate
    #     limits). Full-text, has genuine Dublin coverage. Backend/sources-mcp env. ---
    muse_api_key: str = ""
    muse_max_pages: int = 3  # pages fetched per run (20 results/page); bounds requests

    # --- Workable. Free, no key, full-text, and the largest verified RoI pool we have
    #     (912 jobs on jobs.workable.com's Ireland feed vs 126 across all known Irish
    #     Greenhouse boards). Two modes: the location feed needs no employer list at
    #     all, and per-board polling covers employers discovered by other means. ---
    # Cap on plain-text description kept per ATS posting. Was 6,000 hard-coded in the
    # Greenhouse connector, which cut real postings mid-document: Tines reaches 9,872
    # characters, Amach 8,387, Nearform 7,686. Requirements sit late in a posting, so a
    # low cap removed exactly what qualifying and tailoring need.
    ats_max_text_chars: int = 20000

    workable_location: str = "ireland"  # jobs.workable.com location facet
    workable_page_size: int = 20  # the feed rejects limit>20 with HTTP 400
    workable_max_pages: int = 10  # bounds a run; 10 x 20 covers most of the RoI pool
    # Boards polled per run in per-employer mode (configured + harvested slugs).
    workable_max_boards: int = 40

    # --- Apify (metered scraping platform; LinkedIn actor). OFF by default and hard
    #     capped, same cost-guard treatment as JSearch. Note what this source can and
    #     cannot do: LinkedIn's public pages carry the full description but NO apply
    #     URL (verified against LinkedIn's own guest endpoint — the apply destination
    #     is shown only to authenticated sessions). So these jobs are full-text and
    #     good for qualifying/tailoring, but ats_detect will correctly file their
    #     linkedin.com URLs as `aggregator`, i.e. not auto-applyable. Scraping
    #     LinkedIn also breaches their User Agreement; this is enabled deliberately
    #     by the operator, never by default. ---
    apify_api_token: str = ""
    apify_linkedin_actor: str = "curious_coder~linkedin-jobs-scraper"
    apify_max_results: int = 100  # hard per-run cost cap (~$1 per 1,000 results)
    apify_timeout_seconds: float = 300.0  # actor runs are slow; ~100s for 200 jobs
    apify_country: str = "Ireland"  # LinkedIn location facet

    # --- Scout retrieval tuning ---
    # Derive search terms (roles/keywords/locations) from the user's resume and merge
    # them with the configured search terms, so scouting matches the user's profile.
    scout_profile_search: bool = True
    scout_profile_max_roles: int = 8  # cap profile-derived roles merged into the query
    scout_profile_max_keywords: int = 12  # cap profile-derived skill keywords
    # Poll this many Greenhouse boards per run at most (configured employers + the
    # curated Ireland default set + slugs harvested from fetched apply links).
    scout_max_greenhouse_boards: int = 40

    # --- Config deriver (LLM). On resume (re)upload, an LLM studies the parsed resume
    #     and PROPOSES a market-canonical search config (roles normalized to standard
    #     titles, keywords, locations) for the user to review + confirm. Cheap extraction
    #     tier — canonicalizing already-parsed fields is normalization, not reasoning.
    #     Falls back to the deterministic profile_search derivation when no key is set. ---
    config_deriver_model: str = "claude-haiku-4-5"
    config_deriver_max_tokens: int = 512

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
    crawler_max_pages: int = 3  # cap search-result pages fetched per run
    # Cap how many individual posting pages we open per run to read the full
    # description (politeness + time guard; each is one polite same-domain GET).
    crawler_max_detail_fetches: int = 25
    crawler_min_delay_seconds: float = 2.0  # per-domain minimum delay between requests
    crawler_timeout_seconds: float = 20.0
    crawler_max_text_chars: int = 12000  # cap description text kept per posting

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

    # --- Phase 8 (observability). OpenTelemetry, LOCAL by default: traces export to an
    #     OTLP collector you run (set otel_exporter_otlp_endpoint), else console when
    #     tracing_console_export is on, else spans are created but not exported (quiet
    #     dev). No PII ever on spans — ids/counts/model/cost only. ---
    tracing_enabled: bool = True
    tracing_console_export: bool = False
    otel_exporter_otlp_endpoint: str = ""  # e.g. http://otel-collector:4318/v1/traces
    # Soft per-user LLM spend alert threshold (USD/day); the cost report flags breaches.
    cost_alert_daily_usd: float = 5.0

    # --- Phase 8c (rate limiting). Per-user limits on the expensive/triggering endpoints
    #     (fixed-window over Redis); auth login is limited per-IP against brute force.
    #     Fail-open: a Redis hiccup must never lock a user out. ---
    rate_limit_enabled: bool = True
    rate_limit_window_seconds: int = 60
    rate_limit_trigger_per_min: int = 10   # scout/qualify/analyze/enhance/pipeline/cv.*/embeddings
    rate_limit_chat_per_min: int = 20      # query/ask + concierge chat (interactive LLM)
    rate_limit_auth_per_min: int = 10      # login attempts per IP (brute-force guard)

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
