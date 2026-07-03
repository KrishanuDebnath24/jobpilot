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
