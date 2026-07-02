# JobPilot

An agentic job-hunting cockpit. Upload a resume, describe target roles in chat, and a
pipeline of cooperating agents scouts, qualifies, analyzes, and improves your
applications — all driven from a dashboard + streaming chat.

> **Status: Phase 0 (Scaffold).** Monorepo, Docker Compose, a FastAPI + Next.js
> health-check path, JWT auth with one test user, and CI. Later phases add the data
> model, MCP servers, agents, and the orchestrator — see the build plan in
> `job-hunter-claude-code-prompt (1).md` and running notes in `DECISIONS.md`.

## Repository layout

```
backend/        FastAPI API (auth, health) — Python 3.12
frontend/       Next.js (App Router) + TypeScript + Tailwind
mcp/            MCP servers (Phase 1+; placeholder for now)
orchestrator/   LangGraph pipeline (Phase 4+; placeholder for now)
docker-compose.yml   Postgres+pgvector, Redis, backend, frontend
.github/workflows/ci.yml   Backend tests + frontend build
```

## Quick start

### 1. Configure

```bash
cp .env.example .env
# edit .env — at minimum set a strong JWT_SECRET
```

### 2a. Run with Docker (everything)

```bash
docker compose up --build
```

- Backend:  http://localhost:8000  (health at `/health`, docs at `/docs`)
- Frontend: http://localhost:3000  (shows live backend health)

### 2b. Run locally (no Docker) — fastest for the Phase 0 health check

**Backend**
```bash
cd backend
python -m venv .venv
# Windows PowerShell:  .venv\Scripts\Activate.ps1
# macOS/Linux:         source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

**Frontend** (in a second terminal)
```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:3000 — the page pings the backend and shows **API: ok**.

## Verify the health check

```bash
curl http://localhost:8000/health
# {"status":"ok","service":"jobpilot-backend","version":"0.1.0"}
```

## Auth smoke test (one test user)

Credentials come from `.env` (`TEST_USER_EMAIL` / `TEST_USER_PASSWORD`).

```bash
# Log in → get a JWT
curl -s -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"test@jobpilot.dev","password":"changeme123"}'

# Call a protected route with the token
curl -s http://localhost:8000/auth/me -H "Authorization: Bearer <TOKEN>"
```

## Tests

```bash
cd backend && pytest -q
```
