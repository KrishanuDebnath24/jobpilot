# Phase 2 Work Order — Scout, Source Connectors, Search Config & Dedupe

> Hand this to Claude Code. It expands Phase 2 of the master prompt. Same discipline: sub-steps **in order**, tests + a smoke check after each, **stop for approval at the gate**, log choices in `DECISIONS.md`, commit at the gate.
>
> Treat every **show me** as a hard gate — reproduce it against live output, and watch for stale/cached results. **This is the first phase that makes LLM calls and the first that costs money** (small — see 2a). It is also the first phase where **untrusted external text (job descriptions) reaches an LLM** — the prompt-injection surface is now live, not theoretical.

---

## Prerequisites (do before 2a)

1. **Anthropic API key** — create one in the Console (console.anthropic.com), add billing + the $5 minimum credit, and **set a low spend cap (~$10/mo) with alerts** before any call. Put it in the backend/`sources-mcp` env only: `ANTHROPIC_API_KEY=sk-ant-...`. Never in the frontend, never in git.
2. **Adzuna developer account** — register at developer.adzuna.com (free tier) to get `ADZUNA_APP_ID` + `ADZUNA_APP_KEY`. Backend/`sources-mcp` env only.
3. **Confirm current model IDs/prices** against `platform.claude.com/docs/en/about-claude/models/overview` and `claude.com/pricing` — don't trust third-party tables. Phase 2 only needs the cheap extraction tier.

---

## Decisions locked for this phase (don't re-litigate)

- **Model routing — Haiku by default, config-driven.** The only LLM work in Phase 2 is extracting structured fields from *unstructured crawled HTML*. Route it to **Haiku 4.5** (`claude-haiku-4-5-20251001`). Model choice is a **per-agent config value**, not a constant, so tiers can be promoted later without code changes.
- **Do NOT LLM-normalize already-structured data.** Adzuna and ATS endpoints return structured JSON — normalize those with **deterministic field-mapping code**, no LLM. Spending model tokens to reshape clean JSON is the single biggest cost trap in this phase. The LLM earns its place only on the crawler (HTML → fields) path.
- **No batching/caching yet.** Calls here are low-volume and synchronous. Prompt caching and the Batch API are deferred to **Phase 5** (mass-analysis), where bulk volume first appears. Don't optimize prematurely.
- **Connectors share one interface and one raw shape.** Every connector (`adzuna`, `greenhouse`, `crawl:<board>`) returns the *same* raw job object; one shared normalize + dedupe step consumes all of them. Adding a source later is a new connector, not a new pipeline.
- **All persistence still goes through `db-mcp`.** Scout writes jobs via `upsert_jobs` — no direct ORM access. The Phase 1 boundary holds.
- **Scout runs as a background job.** Long-running fan-out goes through **Celery + Redis** and records a `pipeline_runs` row. Manual trigger for now; automatic orchestration is Phase 4.

---

## Sub-steps (each ends with a "show me")

### 2a — Wire the API key, cost controls & model config
- Load `ANTHROPIC_API_KEY` via the fail-fast config loader (required starting this phase; clear boot error if missing/placeholder).
- Add per-agent model config (default `claude-haiku-4-5-20251001`) and a thin LLM client wrapper (so every call is logged with token counts for cost visibility).
- **Show me:** a minimal Haiku round-trip succeeds; the model is read from config; token usage is logged; the spend cap is set in the Console (state it in `DECISIONS.md`).

### 2b — `config-mcp` + search config
- Stand up `config-mcp` (streamable-HTTP, same pattern as the Phase 1 servers; add to Compose).
- Tools: `get_search_config`, `update_search_config` — user-scoped via the signed context, exactly like `db-mcp`. Fields: `roles[]`, `locations[]`, `keywords[]`, `sources[]` (which connectors are enabled), `target_employers[]` (for ATS), `schedule`.
- Seed a sensible default config for the test user (Ireland-focused).
- **Show me:** create/read/update a search config through `config-mcp`, scoped to the user (a second user can't read it).

### 2c — `sources-mcp` + connector interface + Adzuna connector
- Stand up `sources-mcp` (add to Compose). Define the common connector interface returning the shared raw job shape, plus `list_sources()`.
- Implement the **Adzuna connector** as a thin client: `search_adzuna(config)` → hit the Adzuna API with `ie` country code + the config's roles/locations/keywords → map results into the raw shape. (You may reuse the community Adzuna MCP server instead, but a thin client keeps output-shape control — recommended.) Handle Adzuna's free-tier quota: bound calls, don't fire per keystroke.
- **Show me:** `search_adzuna` returns real Irish jobs in the common raw shape.

### 2d — Normalize + dedupe + persist (the shared tail)
- Deterministic normalize: raw shape → `jobs` table shape (title, company, location, description, url, `source`, `source_type`, `connector`, `raw_json`, `normalized_json`).
- Dedupe: compute `dedupe_hash` from normalized `company + title + location` (and canonicalized URL); collapse duplicates on upsert. Make the whole path **idempotent** — re-running Scout must not create dupes.
- Persist via `db-mcp` `upsert_jobs` with `source_type='api'`, `connector='adzuna'`.
- **Show me:** Adzuna results landing as normalized, deduped rows via `db-mcp` (no direct DB access); re-running produces zero new rows.

### 2e — ATS connector (Greenhouse)
- Add the **Greenhouse connector**: for each `target_employers` entry, hit `boards-api.greenhouse.io/v1/boards/{company}/jobs` → raw shape → the same normalize/dedupe tail. `source_type='ats'`, `connector='greenhouse:<company>'`.
- **Show me:** Greenhouse jobs for a named Irish employer landing normalized + deduped **alongside** Adzuna, and a job that appears in *both* sources collapsing to one row via `dedupe_hash`.

### 2f — Crawler connector (one Irish board) + politeness
- Add **one** polite crawler (Playwright via Crawlee/Scrapy) for a single Irish board that has no API (e.g. an IrishJobs.ie / Jobs.ie search page). Honor `robots.txt` + per-domain rate limits.
- This is the **only** place the LLM is used: pass extracted page HTML/text to **Haiku** to produce the structured fields, with **constrained, structured output** — extraction only. Then the same normalize/dedupe tail. `source_type='crawl'`.
- **Show me:** the crawler landing normalized jobs from one Irish board, respecting robots/rate limits; the Haiku extraction returns structured fields; token cost for the run is logged and small.

### 2g — Scout as a background task + run tracking
- Wrap the full fan-out (enabled connectors → normalize → dedupe → persist) as a **Celery task**. Record a `pipeline_runs` row: `stage='scout'`, status, started/finished, counts (fetched / new / deduped), and any per-connector errors (one failing connector must not kill the run).
- Expose a manual trigger endpoint (`POST /scout/run`); orchestrator auto-trigger is Phase 4.
- **Show me:** trigger a Scout run → it executes in the background → a `pipeline_runs` row captures the counts → jobs are queryable via `db-mcp`; a deliberately broken connector degrades gracefully and is recorded, not fatal.

---

## Phase 2 acceptance (the gate)

Green when **all** hold, then stop for approval before Phase 3:
1. API key wired via fail-fast config; **spend cap set**; Scout's model is config-driven and defaults to Haiku; token usage is logged.
2. `config-mcp` search config CRUD works and is user-scoped.
3. **Adzuna + one ATS feed** land normalized, deduped jobs via `db-mcp` (the master-prompt bar), with cross-source dedupe demonstrably collapsing a shared job.
4. The crawler connector pulls one Irish board politely (robots/rate limits), using constrained Haiku extraction.
5. Scout runs as a tracked background job (`pipeline_runs` row with counts); a failing connector degrades gracefully.
6. Everything re-runs idempotently (no duplicate rows) and writes only through `db-mcp`.

---

## Watch-outs

- **The injection vector is live.** Job descriptions (esp. crawled) are untrusted and now reach Haiku. Keep the extraction prompt extract-only with structured output; never let job text change control flow, tool selection, or which `user_id` a query runs under. Treat connector output as data, not instructions.
- **Guard the spend before the first real run.** A broad config on the crawler path could fan out into many extraction calls. Bound the crawler (max pages/run), keep the cap set, and watch the token log on the first run.
- **Deterministic where the data is already clean.** If you catch yourself sending Adzuna/Greenhouse JSON to a model, stop — that's the cost trap. LLM only on the HTML path.
- **Respect ToS/robots/rate limits; never scrape Indeed or LinkedIn** — get their coverage via Adzuna. Mind Adzuna's free-tier quota.
- **Idempotency is a correctness property, not a nice-to-have** — dedupe must survive re-runs, since Phase 4 will trigger Scout repeatedly.
- **GDPR still applies:** don't harvest recruiter names/emails or other personal data from listings without a lawful basis; job postings themselves are fine.
- **New Compose services** (`config-mcp`, `sources-mcp`) are a verification surface — bring the stack up and confirm each is healthy and reachable, same as Phase 1.
- **Commit at the gate**; log decisions (Adzuna client vs community MCP, dedupe-hash fields, crawler board choice, crawler page cap, model config default).
