# Phase 1 Work Order — Data Layer, MCP Boundary, Resume Ingestion & Erasure

> Hand this to Claude Code. It expands Phase 1 of the master prompt. Same discipline: work through the sub-steps **in order**, run tests + a smoke check after each, and **stop for approval at the phase gate**. Log every non-obvious choice in `DECISIONS.md`, and make a git commit at the end of the phase.
>
> Treat every **show me** as a hard gate: reproduce it against live output before moving on — don't mark a step done on a claim, and watch for stale or cached results masquerading as a passing run. This phase makes **no LLM calls**, so it costs nothing and `ANTHROPIC_API_KEY` stays empty.

---

## Decisions locked for this phase (don't re-litigate)

- **User scoping is bound by the runtime, never by the model.** Every `user_id` used for a query comes from the authenticated session, injected by the backend as a signed per-request context (header/token) the MCP server validates. MCP tools **ignore any `user_id` the caller or an LLM supplies.** This is the whole security model — treat it as the top priority of the phase.
- **MCP transport:** streamable HTTP, so `db-mcp` and `resume-mcp` run as real network services in Compose and the backend calls them over HTTP (proving the boundary from day one). No direct ORM access from paths that should go through MCP.
- **Resume storage:** MinIO (S3-compatible) as a Compose service, behind a `StorageClient` abstraction. Local-FS fallback is fine for bare dev, but keep the interface identical so prod is S3.
- **Resume parsing (no API key needed this phase):** do the deterministic text extraction now (`pdfplumber`/`pypdf` for PDF, `python-docx` for DOCX) plus a rules-based first pass into the typed shape. The LLM structuring pass is **optional and deferred to Phase 2** when the Anthropic key lands — if/when you add it, route it to **Haiku 4.5** (`claude-haiku-4-5-20251001`, the cheap extraction tier) behind the same interface, so nothing above it changes. Store both the raw text and the structured JSON.
- **Embeddings:** define the `vector` columns now but **do not populate them this phase.** Put embedding generation behind a pluggable `Embedder` interface (Voyage recommended) and wire real population when semantic search lands in Phase 3. Don't let this block the gate.
- **Erasure:** hard-delete (not anonymize) for now. Use `ON DELETE CASCADE` FKs toward `user_id` **plus** explicit storage-file cleanup.

---

## Sub-steps (each ends with a "show me")

### 1a — Alembic + real `users` table + DB-backed auth
- Wire Alembic (migrations dir, env, autogenerate config).
- Create `users(id, email UNIQUE, password_hash, created_at)`. Hash with bcrypt or argon2.
- Move auth off the in-memory Phase 0 seed onto the DB. Seed the test user via a seed script (not a migration).
- Add a `/ready` readiness probe that checks DB + Redis connectivity; keep `/health` as pure liveness.
- **Show me:** login round-trips against the DB, `/ready` goes green only when DB+Redis are up, and `alembic downgrade base && alembic upgrade head` runs clean.

### 1b — Full schema migration
- One migration that enables the `pgvector` extension and creates all remaining core tables from the master data model: `resumes`, `search_configs`, `qualifier_configs`, `jobs`, `qualifications`, `analyses`, `suggestions`, `pipeline_runs`, `agent_events`, `chat_sessions`, `messages`.
- Include the fields already speced: `jobs.source_type` / `jobs.connector`, `resumes.consent_at` / `resumes.retention_expires_at`. Define `vector` columns (unpopulated).
- Every user-owned table gets an indexed `user_id` FK with `ON DELETE CASCADE`.
- **Show me:** full schema present; a clean `downgrade base` → `upgrade head` cycle.

### 1c — `db-mcp` with enforced user scoping (the security core)
- Stand up `db-mcp` as a standalone HTTP MCP service; add it to Compose.
- Implement scoped tools: `get_jobs`, `get_job`, `upsert_jobs`, `get_resume`, `get_qualifications`, `get_analyses`, `get_suggestions`, `update_suggestion_status`, and `search_jobs` (stub the semantic path → fall back to keyword match until embeddings exist).
- Enforce the locked scoping rule: `user_id` is derived from the signed request context; any `user_id` passed as a tool argument is ignored (or rejected). Unauthenticated calls are refused.
- **Show me:** a cross-user isolation test where user A cannot read user B's rows **even when A explicitly passes B's `user_id`**, plus rejection of unauthenticated calls.

### 1d — `resume-mcp` + storage + upload + consent
- Build the `StorageClient` (MinIO in Compose, local-FS fallback).
- `resume-mcp`: `parse_resume(file)` → extract text (PDF + DOCX) → deterministic/rules mapping into typed `{contact, skills[], experience[], education[]}` (the LLM structuring pass is a Phase-2 upgrade behind this same interface); `get_structured_resume`.
- Backend `POST /resumes` (multipart): enforce a mime allowlist (PDF/DOCX) + size cap, **require an explicit consent flag**, store the file, and persist `parsed_json` + `consent_at` + a default `retention_expires_at`.
- **Show me:** a real PDF **and** a real DOCX uploaded → structured resume retrievable via `resume-mcp`; a junk/oversized file rejected; upload without consent refused.

### 1e — `delete_user_data` erasure
- `db-mcp` tool `delete_user_data(user_id)` + backend `DELETE /me/data` (chat action comes later). Transactional: cascade every row keyed to the user **and** delete their resume file(s) from storage.
- **Show me:** create a user with a resume + seeded jobs, run erasure, then prove zero rows across every table and zero files in storage remain for that user.

### 1f — Manual job round-trip
- Seed 2–3 jobs via `db-mcp` `upsert_jobs` (bypasses Scout, which doesn't exist yet) with `source_type='seed'`, then `get_jobs` returns them normalized.
- **Show me:** seeded jobs round-tripped cleanly through `db-mcp`.

### 1g — Wire backend → MCP clients
- The backend calls `db-mcp` and `resume-mcp` through an MCP client for these data paths, not direct DB/ORM calls — proving "agents (and the app) only touch data via MCP."
- **Show me:** the upload flow and the job-read flow going through MCP over HTTP, visible in the service logs.

---

## Phase 1 acceptance (the gate)

Green when **all** of these hold, then stop for approval before Phase 2:
1. Migrations up/down clean; auth is DB-backed; `/ready` reflects real DB+Redis health.
2. A PDF and a DOCX resume are parsed and retrievable via `resume-mcp`, with consent recorded.
3. A job round-trips through `db-mcp`.
4. `delete_user_data` verifiably wipes every table **and** the stored file.
5. The cross-user isolation test — including the spoofed-`user_id` case — passes.

---

## Watch-outs

- **The prompt-injection surface opens here.** Resume text now (and job descriptions soon) is untrusted input flowing toward LLM calls. Keep the structuring pass extract-only with structured output, and never let parsed content influence which `user_id` a query runs under.
- **Keep personal data out of git and logs.** No resume files or parsed contents committed; scrub them from structured logs.
- **Don't gold-plate embeddings.** Column + interface only this phase.
- **New Compose services are a verification surface, not an assumption.** Phase 1 adds `db-mcp`, `resume-mcp`, and MinIO to the stack — bring the whole thing up and confirm each is genuinely healthy and that the backend reaches the MCP services over HTTP, the same way you verified Phase 0's four services (health/`ps` checks, not "it should be fine").
- **Commit at the gate** and update `DECISIONS.md` (storage choice, transport choice, hard-delete vs anonymize, embedder deferral).
