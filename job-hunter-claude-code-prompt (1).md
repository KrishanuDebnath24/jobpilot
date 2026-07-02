# Claude Code Build Prompt — "JobPilot" (Agentic Job-Hunting Platform)

> Paste everything below into Claude Code. It is written as a direct instruction set. The **Phase Gates** are load-bearing: do not skip them. Stop and show me working output at the end of each phase before continuing.

---

## 0. Role & Working Agreement

You are the lead engineer building a multi-user, multi-agent job-hunting web app. Work incrementally, phase by phase. At the end of every phase:

1. Run the tests and a manual smoke check.
2. Give me a 5-line summary of what now works and how to run it.
3. **Stop and wait for my approval** before starting the next phase.

Do not scaffold the entire system in one shot. Prefer small, verifiable slices. When you hit a design fork, make a sensible default, note it in `DECISIONS.md`, and keep moving — don't block on me for minor choices.

---

## 1. Product Vision (what we're building)

A personal job-hunting cockpit. The user uploads a resume and describes what they want in plain English via chat. A pipeline of cooperating agents then:

- **Scouts** jobs through a set of source connectors (job-board APIs, ATS endpoints, and — only where needed — polite crawlers),
- **Qualifies** each job against the resume like an ATS (pass/fail + score + reasons),
- **Analyzes** selected or qualified jobs to extract requirements and skill gaps,
- **Suggests** concrete resume changes (skills to add, projects to build, bullet rewrites).

The user drives the whole thing by **chat** and a **dashboard**. They can inspect any of their data, reconfigure the scout/qualifier agents, run individual agents on demand, or let the pipeline run end-to-end automatically with optional human approval gates.

---

## 2. Core User Flows (build toward these)

1. **Onboard** — Upload resume → it's parsed into structured data. Describe target roles/locations/keywords in chat → a search config is created.
2. **Run the pipeline** — "Find me jobs" kicks off Scout → Qualify automatically. Dashboard fills with qualified/disqualified jobs, each with a score and reasoning.
3. **Reconfigure by chat** — "Only include remote roles and weight Python skills higher" updates the scout + qualifier configs. Re-run reflects it.
4. **Analyze & improve** — User selects jobs in the dashboard (or says "analyze all my qualified jobs"). The Analyzer runs, then the Enhancer returns targeted resume suggestions.
5. **Ask anything** — "How many jobs qualified this week? Is the scout still running? What skills come up most?" A dedicated query agent answers over the data and live agent status.
6. **Stay in control** — User can approve/skip pipeline gates and run any single agent manually, all from chat.

---

## 3. Architecture (high level)

```
        ┌──────────────────────── Frontend (Next.js) ───────────────────────┐
        │   Dashboard (jobs, scores, suggestions)   │   Chat (streaming)      │
        └───────────────┬───────────────────────────┴───────────┬────────────┘
                        │ REST + WebSocket/SSE                   │
        ┌───────────────▼────────────────────────────────────────▼───────────┐
        │                       Backend API (FastAPI)                          │
        │   Auth · per-user isolation · chat routing · SSE stream · REST       │
        └───────┬───────────────────────┬───────────────────────┬────────────┘
                │                        │                       │
        ┌───────▼────────┐     ┌─────────▼─────────┐    ┌────────▼─────────┐
        │  Orchestrator  │     │   Agent Runtime   │    │   MCP Servers    │
        │  (LangGraph    │◄───►│  (Claude models,  │◄──►│  db · config ·   │
        │  stateful DAG, │ A2A │  tool-using       │    │  pipeline ·      │
        │  human gates)  │events│ agents)          │    │  sources·resume  │
        └───────┬────────┘     └───────────────────┘    └────────┬─────────┘
                │                                                 │
        ┌───────▼─────────── Redis (queue + pub/sub events) ──────▼──────────┐
        │            Celery workers (crawls, long agent runs)                 │
        └─────────────────────────────┬───────────────────────────────────────┘
                                       │
                        ┌──────────────▼──────────────┐
                        │   PostgreSQL + pgvector      │
                        │  (jobs, resumes, configs,    │
                        │  qualifications, analyses,   │
                        │  suggestions, runs, events)  │
                        └──────────────────────────────┘
```

**Hard rule:** agents never touch the database directly. All data and side-effects go through **MCP servers**, and every MCP tool enforces the caller's `user_id`. This is the security boundary — treat it as non-negotiable.

---

## 4. Tech Stack (use unless you have a strong reason)

- **Backend:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.0 + Alembic.
- **Agents/LLM:** Anthropic Claude models via the API. Use a cheap model for extraction/parsing and a stronger model for reasoning/chat. Cache aggressively.
- **Orchestration:** LangGraph for the stateful pipeline (checkpointing + human-in-the-loop interrupts fit this exactly).
- **Tools/data access:** MCP servers built with the official MCP Python SDK, run as separate processes.
- **Background work:** Celery + Redis (crawls and long agent runs are async jobs). Redis pub/sub for the A2A event bus.
- **DB:** PostgreSQL 16 + `pgvector` (relational data + embeddings for semantic search and RAG).
- **Ingestion (API-first):** the Scout is a set of **source connectors**, not one crawler. Prefer structured APIs — **Adzuna** (free tier, `ie` country code, real-time listings + salary/category data) as the primary feed, plus **ATS public endpoints** (Greenhouse, Lever, Workable, SmartRecruiters, Workday) for named employers. Use **Playwright** (via Crawlee or Scrapy) only for Irish boards with no API, and only within `robots.txt`, per-domain rate limits, and each site's ToS. **Do not scrape Indeed or LinkedIn** — both prohibit it in their ToS, deprecated their public job APIs, and sit behind heavy anti-bot defenses; pull their coverage indirectly via aggregators instead.
- **Frontend:** Next.js (App Router) + TypeScript + Tailwind + shadcn/ui. Streaming chat over SSE/WebSocket; live pipeline status.
- **Auth:** email + JWT (or Auth.js). Multi-user from day one, with strict per-user data isolation.
- **Dev infra:** Docker Compose for Postgres + Redis; `.env` for secrets; pytest + Playwright tests; GitHub Actions CI.
- **Observability:** structured logging + tracing on every agent run (LangSmith or OpenTelemetry).

Record any deviation in `DECISIONS.md` with a one-line rationale.

---

## 5. The Agents

Each agent has a single clear job, typed inputs/outputs, and reaches data only through MCP tools.

| Agent | Job | Reads | Writes |
|---|---|---|---|
| **Scout** | Fan out across source connectors (Adzuna API → ATS endpoints → polite crawlers) from the search config, then normalize + dedupe into one job shape. | `search_config`, `sources-mcp` | `jobs` (via `db-mcp`) |
| **Qualifier** | Score each job vs. resume like an ATS; decide pass/fail with reasons + matched/missing skills. | `resume`, `qualifier_config`, `jobs` | `qualifications` |
| **Analyzer** | Deep-analyze selected/qualified jobs: required skills, keywords, seniority, tech stack, gaps vs. resume. Supports single-job and batch ("mass analysis"). | `jobs`, `resume` | `analyses` |
| **Enhancer** | Turn analysis into concrete suggestions: skills to add, projects to build/highlight, resume bullet rewrites, keyword optimizations. | `analyses`, `resume`, `jobs` | `suggestions` |
| **Query** | Read-only Q&A over all of the user's data **and** the live operational status of the pipeline/agents (RAG + status tools). | everything (read-only) | nothing |
| **Concierge** | The main chat agent and human-in-the-loop front door: reads user data, edits configs, triggers pipeline stages, runs specific agents, routes to other agents. | all MCP servers | configs, pipeline triggers |

**Orchestrator** is *not* an LLM agent — it's a LangGraph state machine that advances jobs through stages by status and pauses at configured human gates.

---

## 6. MCP Servers (the tool/data boundary)

Build each as a standalone MCP server. Every tool takes an authenticated `user_id` and scopes all reads/writes to it.

- **`db-mcp`** — scoped CRUD: `get_jobs`, `get_job`, `upsert_jobs`, `get_resume`, `get_qualifications`, `get_analyses`, `get_suggestions`, `update_suggestion_status`, semantic `search_jobs(query)`.
- **`config-mcp`** — `get_search_config`, `update_search_config`, `get_qualifier_config`, `update_qualifier_config` (roles, sites, keywords, schedule, scoring weights, threshold, must-have/nice-to-have rules).
- **`pipeline-mcp`** — `get_pipeline_status`, `trigger_stage(stage, scope)`, `run_full_pipeline`, `approve_gate(gate_id)`, `skip_gate(gate_id)`, `list_runs`, `get_run(run_id)`.
- **`sources-mcp`** (the connector hub) — one tool per connector behind a common interface: `search_adzuna(query)`, `fetch_ats(company, provider)`, `run_crawl(site, config)`, plus `test_selector(url, selector)` and `list_sources()`. Each connector returns the same raw job shape for the Scout to normalize. Enforces per-source rate limits + `robots.txt` internally. You can reuse the community **Adzuna MCP server** as the Adzuna connector rather than writing it from scratch.
- **`resume-mcp`** — `parse_resume(file)`, `get_structured_resume`, returns normalized skills/experience/education + embedding.

---

## 6.5 Data Sources & Connectors (Ireland)

Build the Scout as connectors in this priority order. Ireland coverage is a config list you grow, not code you rewrite. Track which tier each job came from (`source_type`) so quality and dedupe stay traceable.

**Tier 1 — Aggregator APIs (start here; structured JSON, no crawler maintenance).**
- **Adzuna** — the backbone. Free developer tier, `ie` country code, returns title/company/location/description + salary + category with real-time updates and filtering; aggregates thousands of Irish sources. A community MCP server already exists — reuse it.
- **Careerjet** (careerjet.ie) and **Jooble** — secondary partner/affiliate APIs worth evaluating for extra Irish coverage.
- **SerpApi Google Jobs** — pulls Google-for-Jobs results (which aggregate Indeed, LinkedIn, Workday, etc.), giving you their coverage without scraping them directly.
- If budget allows: **TheirStack** (aggregates 315k+ sources incl. 16k+ ATS platforms, auto-deduped, filter by company size/funding/tech stack), or managed providers **Bright Data / Oxylabs**.

**Tier 2 — ATS public endpoints (the cleanest, most stable data).** Ireland is dense with multinationals and pharma whose careers pages run on a handful of ATS platforms with public JSON. Keep a list of target employers and hit these directly:
- Greenhouse — `https://boards-api.greenhouse.io/v1/boards/{company}/jobs`
- Lever — `https://api.lever.co/v0/postings/{company}`
- Workable, SmartRecruiters, Workday — each exposes queryable public endpoints.

**Tier 3 — Ireland-specific boards (crawl politely; check `robots.txt` + ToS first).**
- **IrishJobs.ie** (Irish Times network) and **Jobs.ie** — the two big indigenous boards.
- **Rezoomo.com** — major Irish ATS/board (SMEs, healthcare, public bodies).
- **publicjobs.ie** — Public Appointments Service (all civil/public-service roles).
- **educationposts.ie** (teaching), **careersportal.ie**, and agencies (CPL, Morgan McKinley, Sigmar, Hays).

**Do not scrape:** Indeed and LinkedIn. Public job APIs deprecated, ToS prohibit automated extraction, and both sit behind heavy anti-bot (Cloudflare/CAPTCHA). Get their listings via Tier 1 aggregators instead.

---

## 7. Agent-to-Agent Communication & Auto-Pipeline

Agents cooperate through a shared **job envelope** (typed payload referencing rows by id, never dumping full blobs) and a **Redis pub/sub event bus**. The Orchestrator subscribes and advances the flow so the user never has to hand-run stages:

```
scout.completed    → orchestrator triggers Qualifier on the new jobs
qualify.completed  → [optional gate] → auto-trigger Analyzer on the qualified set
analyze.completed  → trigger Enhancer → suggestions.ready → notify UI
*.failed           → mark run failed, surface error in chat + dashboard, offer retry
```

Every stage writes a `pipeline_runs` record (stage, status, timestamps, input/output refs, error) and emits an `agent_events` entry. The dashboard and Query agent both read these so status is always truthful and live. Make stage triggers **idempotent** and safe to retry.

---

## 8. Human-in-the-Loop

The user stays in control entirely through chat + dashboard:

- **Configure:** create/edit search config and qualifier config in natural language; upload/replace resume.
- **Run:** trigger any single stage, or the full pipeline, or run one specific agent on demand.
- **Gate:** configurable approval gates (e.g. review scouted jobs before qualifying; review qualified set before analysis; approve suggestions before they're marked "applied"). Each gate can be toggled on/off. When on, LangGraph interrupts and waits for `approve_gate`/`skip_gate` from chat.
- **Select:** pick jobs in the dashboard for targeted analysis, or say "analyze all qualified jobs" for a batch run.

---

## 9. Data Model (starting schema)

Design in Alembic migrations. `user_id` on every user-owned table; index it and filter on it in *every* query.

- `users(id, email, ...)`
- `resumes(id, user_id, file_ref, parsed_json, embedding, consent_at, retention_expires_at)` — resume is personal data; record consent and support deletion
- `search_configs(id, user_id, roles[], locations[], sites[], keywords[], schedule)`
- `qualifier_configs(id, user_id, weights_json, threshold, must_have[], nice_to_have[])`
- `jobs(id, user_id, source, source_type, connector, url, title, company, location, description, raw_json, normalized_json, embedding, dedupe_hash, status)` — `source_type` ∈ {api, ats, crawl}; `connector` names the feed (e.g. `adzuna`, `greenhouse:stripe`)
- `qualifications(id, job_id, score, decision, reasons_json, matched_skills[], missing_skills[])`
- `analyses(id, user_id, job_id NULL, batch_id NULL, extracted_json, gaps_json, seniority)`
- `suggestions(id, user_id, job_id NULL, batch_id NULL, skills_to_add[], projects_json, bullet_rewrites_json, status)`
- `pipeline_runs(id, user_id, stage, status, started_at, finished_at, input_ref, output_ref, error)`
- `agent_events(id, user_id, type, payload_json, created_at)`
- `chat_sessions(id, user_id, ...)`, `messages(id, session_id, role, content, tool_calls_json, created_at)`

**Erasure:** implement a single `delete_user_data(user_id)` path (exposed as a `db-mcp` tool and a UI/chat action) that hard-deletes or anonymizes every row keyed to that user across all tables above, plus the stored resume file. This is a GDPR requirement, not a nice-to-have.

---

## 10. Build Plan (phased — respect the gates)

**Phase 0 — Scaffold.** Monorepo (`/backend`, `/frontend`, `/mcp`, `/orchestrator`). Docker Compose (Postgres+pgvector, Redis). FastAPI + Next.js "hello world" wired end-to-end. Auth with one test user. CI green. → *Show me a health check passing across both services.*

**Phase 1 — Data + `db-mcp` + resume + erasure.** Migrations for all core tables. `db-mcp` and `resume-mcp` with resume upload → parse → structured data, capturing consent on upload. Implement `delete_user_data(user_id)` and prove it wipes every table + the stored file. Manual job insert to prove reads. → *Show me a parsed resume, a job round-tripped through MCP, and a full user-data erasure.*

**Phase 2 — Scout (connectors).** `sources-mcp` + Scout agent + `config-mcp` for search config. Build the **Adzuna API connector first** (fastest path to real Irish data), then **one ATS connector** (e.g. Greenhouse), and only then a **single polite crawler** for one Irish board. Normalize all three into one job shape + dedupe. → *Show me Adzuna + one ATS feed landing normalized, deduped jobs in the DB.*

**Phase 3 — Qualifier + Dashboard v1.** Qualifier agent + qualifier config + `qualifications`. Dashboard table of qualified/disqualified with score + reasons. → *Show me the dashboard reflecting a real qualification run.*

**Phase 4 — Orchestrator.** LangGraph wiring Scout→Qualify via `pipeline-mcp` and the event bus. Live status in the UI. → *Show me an end-to-end auto-run I didn't hand-hold.*

**Phase 5 — Analyzer + Enhancer.** Both agents + suggestions UI, single-job and batch. → *Show me suggestions generated from selected jobs and from a batch.*

**Phase 6 — Concierge chat + gates.** Chat agent that configures + triggers everything; human-in-the-loop approval gates. → *Show me reconfiguring and running the pipeline entirely by chat.*

**Phase 7 — Query agent.** RAG over data + live status tools. → *Show me it answering data and "is X running?" questions correctly.*

**Phase 8 — Hardening.** Tracing, rate limiting, LLM cost controls, test coverage, deploy guide. → *Show me the dashboards, traces, and a deploy runbook.*

---

## 11. Conventions & Guardrails

- **Isolation is security:** every query is scoped by `user_id`; add a test that proves user A can never read user B's data.
- **GDPR by design (Ireland/EU):** you're a data controller under the Irish DPC. Capture consent for the user's resume, keep a working `delete_user_data` erasure path, and apply data minimization. Job *postings* are generally fine to store; do **not** harvest recruiter names/emails or other personal data from listings without a lawful basis.
- **MCP is the only data path for agents** — no direct DB access from agent code.
- **Typed everywhere:** Pydantic on the backend, generated TS types on the frontend.
- **Test each phase; don't advance on red.** Include at least one integration test per phase.
- **API-first ingestion:** prefer official/aggregator APIs and ATS endpoints; crawl only where there's no API, and then honor `robots.txt`, per-domain rate limits, and ToS. Never scrape Indeed or LinkedIn. Make every connector idempotent.
- **Cost control:** cheap models for extraction, cache prompts, batch where possible; log token spend per run.
- **Observability:** structured logs + a trace for every agent run and pipeline stage.
- **Secrets in `.env`, never committed.** Keep a `.env.example`.
- **Keep `DECISIONS.md`** current with every non-obvious choice.

---

## 12. Do This First

Execute **Phase 0 only**: set up the monorepo, Docker Compose, the FastAPI+Next.js health-check path, auth with one test user, and CI. Then stop, give me the run instructions and the 5-line summary, and wait for approval before Phase 1.
