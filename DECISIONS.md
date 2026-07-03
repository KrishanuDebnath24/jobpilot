# DECISIONS

Running log of non-obvious engineering choices. One line + rationale each.

## Phase 0 — Scaffold

- **Monorepo layout `/backend /frontend /mcp /orchestrator`.** Matches the target
  architecture; `mcp/` and `orchestrator/` are placeholders until Phase 1+ so the
  tree is stable from day one.
- **Backend deps via `requirements.txt` (not Poetry/uv).** Fewer moving parts for a
  Dockerized FastAPI service; easy to pin. Revisit if the team wants a lockfile tool.
- **Auth password hashing with the `bcrypt` library directly (not `passlib`).**
  `passlib`'s bcrypt backend is unmaintained and warns against modern `bcrypt` 4.x.
  Calling `bcrypt` directly is simpler and avoids the version-detection breakage.
- **JWT via `PyJWT` + OAuth2 password flow.** Standard, dependency-light, and plays
  cleanly with FastAPI's `OAuth2PasswordBearer`.
- **Phase 0 test user is in-memory (seeded from `TEST_USER_EMAIL/PASSWORD`).** The
  `users` table + real persistence arrive in Phase 1; keeping auth working end-to-end
  now without a DB lets the health-check slice stay small and verifiable.
- **`/health` is a pure liveness check (no DB dependency).** Phase 0's gate is
  "health passing across both services"; DB/Redis readiness probes land in Phase 1
  once migrations exist. Compose still starts Postgres+pgvector and Redis so the
  environment is ready.
- **Tailwind v3.4 (not v4).** v3 config is stable and well-documented; avoids v4's
  in-flux config story for a scaffold. shadcn/ui is deferred to the first real UI
  phase (Phase 3 dashboard).
- **Frontend calls the API from the browser via `NEXT_PUBLIC_API_URL`.** Simplest
  wiring for the health check; a server-side proxy/BFF can come later if needed.
- **Postgres image `pgvector/pgvector:pg16`.** Ships Postgres 16 with the `pgvector`
  extension preinstalled, exactly what the data model (embeddings) needs in Phase 1.
- **Next.js pinned to `14.2.35` (latest patched 14.2.x); framework-major bump deferred
  to Phase 8.** 14.2.35 clears the Dec-2025 critical advisory. Two residual transitive
  advisories remain, both accepted for now: (1) `next` "high" DoS via the Image
  Optimizer `remotePatterns` — **not applicable**, we use no `next/image`/remote
  patterns; (2) `postcss` "moderate" XSS in CSS stringify — **build-time only**, never
  shipped to the client runtime. npm's only offered fix is Next 16 (breaking major,
  pulls React 19), which is out of scope for a scaffold and slots naturally into the
  Phase 8 hardening pass.

## Phase 1 — Data layer, MCP boundary, resume ingestion & erasure

- **Shared code lives in an installable package `packages/core` (`jobpilot_core`).**
  Both the backend and the MCP services depend on it (SQLAlchemy models, async DB
  session, repositories, security, storage, resume parsing, MCP auth). Each service
  Dockerfile builds from the repo root and `pip install ./packages/core`.
- **Runtime-injected `user_id` is the security boundary.** The backend mints a
  short-lived HS256 **MCP context token** per outbound call (subject = authenticated
  user_id, signed with `MCP_SHARED_SECRET`, default = `JWT_SECRET`). A pure-ASGI
  middleware on each MCP service validates it and binds the user_id in a contextvar;
  tools read it via `require_user_id()`. **Any `user_id` passed as a tool argument is
  ignored** (`get_jobs`/`delete_user_data` accept one purely to prove it's dropped).
  Unauthenticated calls are refused. Verified over real HTTP incl. the spoofed-id case.
- **MCP transport: streamable HTTP with `stateless_http=True`.** Stateless keeps each
  tool call within the request's ASGI task, so the auth contextvar propagates reliably
  (verified). Servers run as real Compose services; the backend calls them over HTTP.
- **Async SQLAlchemy 2.0 (asyncpg) + async Alembic.** Matches FastAPI/FastMCP. Tests
  reset the cached engine per test because pytest-asyncio gives each test a fresh loop
  and asyncpg connections must not outlive their loop.
- **`user_id` added to `qualifications`, `analyses`, `suggestions`, `messages`** beyond
  the master §9 sketch, so every user-owned table scopes uniformly and cascades from
  `users` on delete. All FKs toward `user_id` are `ON DELETE CASCADE`.
- **Erasure = hard delete + explicit storage cleanup.** `delete_user_data` deletes the
  user's stored files by prefix (best-effort, outside the tx), then deletes the `users`
  row so every table cascades, then verifies zero rows remain in-transaction. Verified:
  2 MinIO files → `files_deleted:2` → 0, all tables 0.
- **Resume storage: MinIO (S3) behind a `StorageClient`; local-FS fallback** for bare
  dev/tests (`STORAGE_BACKEND=local`). `file_ref` is the object key so erasure deletes
  precisely by prefix.
- **Resume parsing is deterministic (no LLM this phase).** pdfplumber/pypdf (PDF),
  python-docx (DOCX), then a rules-based pass into the typed shape. Raw text + structured
  JSON are both stored. The Phase-2 LLM structuring pass (Haiku 4.5,
  `claude-haiku-4-5`) slots behind the same interface.
- **Embeddings deferred.** `vector(1024)` columns exist but stay NULL; population lands
  with semantic `search_jobs` in Phase 3 (which currently falls back to keyword match).
  Dimension is fixed at 1024 now to avoid a later migration.
- **Consent required to persist a resume.** `POST /resumes` refuses without an explicit
  consent flag (422); `consent_at` + `retention_expires_at` are recorded on the row.
- **FastAPI upgraded 0.115.6 → 0.139.0.** The MCP SDK (`mcp` 1.28.1) requires modern
  starlette (1.3.1); FastAPI 0.139 accepts `starlette>=0.46`, resolving the conflict.
- **DB-backed auth replaces the Phase-0 in-memory user.** The test user is created by
  `scripts.seed_user` (a seed script, not a migration). `/ready` checks DB+Redis;
  `/health` stays pure liveness.
- **Local dev vs Compose hostnames.** `.env` uses compose service names (`db`, `redis`,
  `minio`); running processes on the host needs `localhost` URLs (override via env).

## Phase 2 — Scout, source connectors, search config & dedupe

- **Scout's model is config-driven, defaults to Haiku 4.5** (`claude-haiku-4-5-20251001`,
  `settings.scout_extractor_model`). The only LLM work is *extraction* (crawled HTML →
  fields); tiers can be promoted later without code changes. All calls route through one
  wrapper (`jobpilot_core.llm`) that logs `input/output tokens` + an estimated cost.
- **Spend controls.** A **Console spend cap (~$10/mo) must be set by the account owner** —
  it can't be set from code. Code-level guards back it up: `llm_max_output_tokens` (per-call
  output cap), `crawler_max_pages` + `crawler_max_text_chars` (bound extraction calls and
  their input size), and `adzuna_max_locations` (bound the free-tier quota). Whole-run token
  usage is logged and recorded on the `pipeline_runs` row.
- **LLM only on the crawl path; deterministic everywhere else.** Adzuna and Greenhouse return
  structured JSON, normalized by plain field-mapping code (`jobpilot_core.scout`). Sending
  clean JSON to a model is the phase's cost trap — avoided. Haiku is used *only* to extract
  fields from unstructured board HTML.
- **Adzuna has no Republic-of-Ireland coverage.** The live API rejects `ie`
  (`UNSUPPORTED_COUNTRY`; supported: at, au, be, br, ca, ch, de, es, fr, gb, in, it, mx, nl,
  nz, pl, sg, us, za). Adzuna *does* cover **Northern Ireland** under `gb` (Belfast/Derry), so
  `adzuna_country` defaults to `gb`; RoI (Dublin) coverage comes from the Greenhouse + crawler
  connectors. `adzuna_country` stays config-driven so `ie` activates automatically if Adzuna
  ever adds it. The connector is a thin async client (not the community MCP server) to keep
  full control of the output shape.
- **One raw shape + one normalize/dedupe tail.** Every connector returns
  `jobpilot_core.scout.RawJob`; `normalize_raw` maps any of them into the `jobs` shape. Adding
  a source is a new connector, not a new pipeline.
- **Dedupe key = normalized `company + title + coarse-location`; URL deliberately excluded.**
  A job carried by *both* Adzuna and an ATS has different URLs, so hashing the URL would
  prevent the required cross-source collapse. Location is coarsened to its first comma token
  (`Dublin, Ireland` and `Dublin` → `dublin`) so the same job dedupes across feeds that spell
  location differently. URL is a fallback basis only when company+title are both absent (some
  crawled listings). `upsert_jobs` collapses duplicates both against existing rows and within
  a single batch, making re-runs idempotent (a correctness property — Phase 4 re-triggers
  Scout).
- **Crawler: one Irish board (irishjobs.ie), robots-respecting, browser UA.** The board serves
  server-rendered HTML but **disconnects non-browser user-agents at the network layer**, so
  the crawler sends a mainstream browser UA. Politeness is still enforced where it's
  declarable/enforceable: we honor `robots.txt` (the site's machine-readable crawl policy — its
  search path is allowed), a per-domain minimum delay, and a hard page cap. Page text → Haiku
  with a **forced tool call + strict schema** (extract-only): the page is untrusted, so the
  model must answer via the tool and is instructed to treat text as data and never follow
  instructions in it — the live prompt-injection surface is contained.
- **config-mcp and sources-mcp are new first-class MCP services.** config-mcp owns search
  configs (direct DB, user-scoped exactly like db-mcp). sources-mcp is the connector hub; it
  holds no DB state but still **requires a valid signed context** so anonymous callers can't
  burn our Adzuna quota / Anthropic budget, and it **fail-fasts at boot** without a real
  Anthropic key (the phase's fail-fast config requirement, enforced where LLM calls happen).
- **Scout persists only through db-mcp; runs as a Celery task.** The Celery worker (Redis
  broker/result) calls config-mcp → sources-mcp → db-mcp over HTTP with minted context tokens,
  exactly like the backend. `POST /scout/run` enqueues; `GET /scout/runs` lists tracked runs.
  A `pipeline_runs` row records stage/status/timings and counts (fetched/new/deduped + per-
  connector tallies, errors, and LLM usage). **One failing connector is recorded, not fatal.**
- **redis pin 8.0.1 → 6.4.0.** Celery's `kombu[redis]` extra caps `redis<6.5`; we only use the
  async client for a readiness ping (API stable across 5.x/6.x).
- **Secret hygiene.** `ANTHROPIC_API_KEY` + Adzuna creds live only in the gitignored `.env`
  and are consumed by sources-mcp (and the backend/worker envs); the **frontend never receives
  them** (its container gets only `NEXT_PUBLIC_API_URL`). Adzuna authenticates via query params,
  so its (read-only, free-tier) key can appear in HTTP client logs — keep client log level at
  WARNING in production to avoid logging it.
