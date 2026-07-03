# JobPilot

An agentic job-hunting cockpit. Upload a resume, describe target roles in chat, and a
pipeline of cooperating agents scouts, qualifies, analyzes, and improves your
applications — all driven from a dashboard + streaming chat.

> **Status: Phase 1 (Data layer, MCP boundary, resume ingestion & erasure).**
> Full data model + migrations, two MCP services over HTTP (`db-mcp`, `resume-mcp`)
> with runtime-injected `user_id` scoping, deterministic resume parsing (PDF/DOCX) to
> MinIO with consent, and GDPR hard-delete erasure. **No LLM calls this phase.**
> See the build plan and `directions/` work orders; running notes in `DECISIONS.md`.

## Repository layout

```
packages/core/    Shared package `jobpilot_core` — SQLAlchemy models, async DB,
                  repositories, security, storage, resume parsing, MCP auth
                  + Alembic migrations (packages/core/migrations)
backend/          FastAPI API (auth, /ready, resume upload, jobs, erasure)
mcp/db_mcp/       db-mcp   — scoped CRUD + delete_user_data (streamable HTTP)
mcp/resume_mcp/   resume-mcp — parse_resume + get_structured_resume
orchestrator/     LangGraph pipeline (Phase 4+; placeholder)
frontend/         Next.js (App Router) + TypeScript + Tailwind
docker-compose.yml   db(pgvector) · redis · minio · migrate · db-mcp · resume-mcp · backend · frontend
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
- db-mcp:   http://localhost:8001/mcp   · resume-mcp: http://localhost:8002/mcp
- MinIO console: http://localhost:9001  (minioadmin / minioadmin)
- Frontend: http://localhost:3000

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
