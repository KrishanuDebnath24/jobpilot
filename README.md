# JobPilot

An agentic job-hunting cockpit. Upload a resume, describe target roles in chat, and a
pipeline of cooperating agents scouts, qualifies, analyzes, and improves your
applications — all driven from a dashboard + streaming chat.

> **Status: Phase 4 (Orchestrator — autonomous Scout→Qualify + live status).**
> A **LangGraph** control plane sequences the existing Celery stages with no human between
> them: one trigger runs Scout→Qualify, checkpointed to Postgres (`thread_id = pipeline_id`)
> so a crash mid-run **resumes** instead of restarting. Progression is event-driven (Redis
> pub/sub) but **reconciled** against persisted `pipeline_runs` status; a **one-active-pipeline
> per user** guard is enforced at the DB; **Celery beat** fires pipelines on a schedule; and the
> dashboard shows **live** stage progress over SSE. Builds on Phase 3 (Qualifier + Dashboard),
> Phase 2 (Scout + connectors) and Phase 1 (data layer, MCP boundary, GDPR). Phase 4 adds
> **zero new LLM cost** (the orchestrator is a state machine). See `directions/` + `DECISIONS.md`.

## Repository layout

```
packages/core/    Shared package `jobpilot_core` — SQLAlchemy models, async DB,
                  repositories, security, storage, resume parsing, MCP auth,
                  LLM wrapper (llm.py), Scout normalize/dedupe (scout.py),
                  Qualifier scorer (qualifier.py) + Alembic migrations
backend/          FastAPI API (auth, /ready, resume, jobs, erasure, /scout/*,
                  /qualify/*, /dashboard/*, /pipeline/* incl. SSE stream), Scout +
                  Qualify runners, LangGraph orchestrator (orchestrator.py), Redis
                  event bus, beat scheduler + Celery tasks (tasks.py, asyncrun.py)
mcp/db_mcp/       db-mcp   — scoped CRUD, pipeline-run tracking, get_stage_status
mcp/resume_mcp/   resume-mcp — parse_resume + get_structured_resume
mcp/config_mcp/   config-mcp — get/update search + qualifier config (user-scoped)
mcp/sources_mcp/  sources-mcp — connector hub: search_adzuna, fetch_greenhouse,
                  run_crawl (Haiku extraction), aggregators, list_sources
mcp/pipeline_mcp/ pipeline-mcp — control surface: run_full_pipeline, get_pipeline_status,
                  trigger_stage, list_runs, get_run (+ one-active-pipeline guard)
packages/core/    …plus events.py (pub/sub bus) + qualifier.py (Phase 3)
frontend/         Next.js (App Router) + TypeScript + Tailwind (live pipeline view)
docker-compose.yml   db(pgvector) · redis · minio · migrate · db-mcp · resume-mcp ·
                     config-mcp · sources-mcp · pipeline-mcp · backend ·
                     worker(data plane) · orchestrator-worker(control plane) ·
                     beat(scheduler) · frontend
```

**Security boundary:** agents and the app touch job/resume *data* only through the MCP
services. Each MCP tool derives `user_id` from a signed request-context token — never
from a tool argument. See `DECISIONS.md`.

## Quick start (Docker — everything)

```bash
cp .env.example .env          # then set a strong JWT_SECRET
docker compose up --build
```

Order: infra → `migrate` (applies schema + seeds the test user) → MCP services →
backend → frontend.

- Backend:  http://localhost:8000  (`/health`, `/ready`, docs at `/docs`)
- db-mcp: `:8001/mcp` · resume-mcp: `:8002/mcp` · config-mcp: `:8003/mcp` · sources-mcp: `:8004/mcp` · pipeline-mcp: `:8005/mcp`
- MinIO console: http://localhost:9001  (minioadmin / minioadmin)
- Frontend: http://localhost:3000

> **Phase 2 needs credentials** (backend/`sources-mcp` env only, never the frontend or git):
> `ANTHROPIC_API_KEY` (set a low Console spend cap first) and `ADZUNA_APP_ID`/`ADZUNA_APP_KEY`
> from developer.adzuna.com. `sources-mcp` refuses to start without a real Anthropic key.
> Adzuna covers Northern Ireland via `gb` (not the Republic of Ireland — see `DECISIONS.md`).
>
> **RoI coverage (addendum)** comes from aggregator connectors — free & on by default:
> `JOOBLE_API_KEY` (jooble.org/api/about) and `CAREERJET_API_KEY` (careerjet.com/partners).
> `RAPIDAPI_KEY` enables the metered JSearch connector (Indeed/LinkedIn reach) — **off by
> default**; subscribe to JSearch on RapidAPI and add `jsearch` to your `sources[]` to use it.
> Any connector without its key degrades gracefully (recorded on the run, never fatal).

Upload a resume first (`POST /resumes` with `consent=true`) — it seeds a conservative qualifier
config. Then run the **autonomous pipeline** (Scout→Qualify with no manual step between):

```bash
curl -X POST http://localhost:8000/pipeline/run    -H "Authorization: Bearer $TOKEN"  # -> {pipeline_id,...}
curl      http://localhost:8000/pipeline/status  -H "Authorization: Bearer $TOKEN"  # live stage + counts
curl      http://localhost:8000/dashboard/jobs   -H "Authorization: Bearer $TOKEN"  # jobs + qualifications
# individual stages still available: POST /scout/run · POST /qualify/run · POST /pipeline/stage/{scout|qualify}
```

A second `/pipeline/run` while one is active is **rejected** (one active pipeline per user). Set
`search_configs.schedule` (e.g. `hourly`, `daily`, `15m`) to have **beat** fire pipelines
automatically. Open the **dashboard** at http://localhost:3000 (sign in with the seeded user) to
watch Scout→Qualify progress **live** (SSE) and browse qualified/disqualified jobs with scores,
fact-citing reasons, filters, and sort.

## Local dev (processes on the host against dockerized infra)

Use `localhost` hostnames (the `.env` uses compose service names). Bring up infra:

```bash
docker compose up -d db redis minio
```

Install deps once (single dev venv):

```bash
cd backend && python -m venv .venv && .venv\Scripts\Activate.ps1   # or source .venv/bin/activate
pip install -e ../packages/core && pip install -r requirements.txt
```

Set localhost URLs, migrate + seed, then run the services (three terminals):

```bash
# from repo root
export DATABASE_URL=postgresql+asyncpg://jobpilot:jobpilot_dev_pw@localhost:5432/jobpilot
alembic upgrade head
cd backend && python -m scripts.seed_user

# db-mcp  (cwd must be mcp/ so `import mcp` resolves to the SDK)
cd mcp && DATABASE_URL=... S3_ENDPOINT_URL=http://localhost:9000 uvicorn db_mcp.server:app --port 8001
# resume-mcp
cd mcp && DATABASE_URL=... S3_ENDPOINT_URL=http://localhost:9000 uvicorn resume_mcp.server:app --port 8002
# backend
cd backend && DATABASE_URL=... REDIS_URL=redis://localhost:6379/0 \
  DB_MCP_URL=http://localhost:8001/mcp RESUME_MCP_URL=http://localhost:8002/mcp \
  uvicorn app.main:app --port 8000
```

## Smoke tests

```bash
curl http://localhost:8000/health          # {"status":"ok",...}
curl http://localhost:8000/ready            # {"ready":true,"db":true,"redis":true}

# login (seeded test user), then upload a resume with consent
TOKEN=$(curl -s -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"test@jobpilot.dev","password":"changeme123"}' | jq -r .access_token)
curl -X POST http://localhost:8000/resumes -H "Authorization: Bearer $TOKEN" \
  -F "file=@resume.pdf;type=application/pdf" -F "consent=true"
curl http://localhost:8000/jobs -H "Authorization: Bearer $TOKEN"

# GDPR erasure (wipes every table + stored files for this user)
curl -X DELETE http://localhost:8000/me/data -H "Authorization: Bearer $TOKEN"
```

## Tests

The suite spawns the MCP services and exercises the real HTTP boundary (isolation incl.
spoofed `user_id`, unauthenticated rejection, resume PDF+DOCX, erasure of rows + files).
Requires a reachable Postgres (skips cleanly otherwise).

```bash
export DATABASE_URL=postgresql+asyncpg://jobpilot:jobpilot_dev_pw@localhost:5432/jobpilot
cd backend && pytest
```
