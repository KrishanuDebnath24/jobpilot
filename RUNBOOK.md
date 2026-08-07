# JobPilot — Deploy & Operations Runbook (Phase 8h)

Launch-readiness operations guide: bring the full stack up healthy, back it up, and roll
it back. Pairs with `DECISIONS.md` (why) and `README.md` (dev quick-start).

---

## 1. Services

One Docker Compose stack (`docker-compose.yml`). Bring-up dependency order matters:

| Tier | Service | Role | Depends on |
|------|---------|------|-----------|
| infra | `db` | Postgres 16 + pgvector | — |
| infra | `redis` | Celery broker/result + rate-limit + pub/sub | — |
| infra | `minio` | S3-compatible object storage (resumes + rendered CVs) | — |
| schema | `migrate` | runs `alembic upgrade head` + seeds the test user, then exits | db |
| mcp | `db-mcp` `:8001` | scoped CRUD, run tracking, embeddings I/O, erasure | db, minio |
| mcp | `resume-mcp` `:8002` | resume parse + storage | db, minio |
| mcp | `config-mcp` `:8003` | search/qualifier/gate config | db |
| mcp | `sources-mcp` `:8004` | connector hub (needs a real Anthropic key to boot) | — |
| mcp | `pipeline-mcp` `:8005` | orchestrator control surface | db, redis |
| app | `backend` `:8000` | FastAPI API + agent runners | db, redis, all mcp |
| worker | `worker` | data plane (scout/qualify/analyze/enhance/cv/embed) | db, redis, mcp |
| worker | `orchestrator-worker` | control plane (LangGraph pipeline) — **separate queue** | db, redis |
| sched | `beat` | Celery beat scheduler tick (singleton) | redis |
| ui | `frontend` `:3000` | Next.js standalone | backend |

**Why the two workers are separate:** the orchestrator *awaits* stages; if it shared a
pool with the stages it waits on, it would starve them (Phase 4). Keep the `orchestrator`
queue on its own worker. **`beat` must be a singleton** — never scale it >1 (duplicate
scheduled pipelines).

---

## 2. Prerequisites & secrets

1. `cp .env.example .env` and fill in. **Required for a real deployment:**
   - `JWT_SECRET` — `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
   - `ANTHROPIC_API_KEY` (`sk-ant-…`) — **set a low Console spend cap first** (~$10/mo).
     `sources-mcp` and the LLM agents fail-fast without it.
   - `POSTGRES_PASSWORD`, `S3_ACCESS_KEY`/`S3_SECRET_KEY` — change from dev defaults.
   - Connectors: Workable, Greenhouse and the crawler need **no keys**. Optional metered
     sources (off unless listed in `sources[]`): `RAPIDAPI_KEY`/`JSEARCH_API_KEY`,
     `APIFY_API_TOKEN`.
2. **Secret hygiene** (enforced by `test_phase8_security.py`): the real `.env` is gitignored
   and never committed; the **frontend only ever gets `NEXT_PUBLIC_*`** (never a server
   secret); keep client log level at WARNING in prod (metered-source keys can appear in
   query params — Apify passes its token as `?token=`).
3. **Observability (optional, Phase 8a):** set `OTEL_EXPORTER_OTLP_ENDPOINT` to a collector
   you run to ship traces; else leave empty (local, no egress). No PII is ever traced.

---

## 3. Bring-up

```bash
cp .env.example .env            # then edit secrets
docker compose up --build -d    # respects depends_on ordering
docker compose logs -f migrate  # confirm "alembic upgrade head" succeeded, container exits 0
```

Order is automatic via `depends_on`: infra → `migrate` → MCP services → backend →
workers/beat → frontend.

### Health / readiness gates (verify each tier before trusting the next)

```bash
curl -fsS localhost:8000/health   # {"status":"ok"} — liveness (no deps)
curl -fsS localhost:8000/ready    # {"ready":true,"db":true,"redis":true} — readiness
for p in 8001 8002 8003 8004 8005; do curl -fsS "localhost:$p/mcp" -o /dev/null && echo "mcp $p up"; done
docker compose ps                 # every service "healthy"/"running"; migrate "exited (0)"
```

- `/health` = pure liveness (use for the container healthcheck / LB).
- `/ready` = DB + Redis reachable (use for readiness gating / not-ready → 503).
- Workers: `docker compose logs worker orchestrator-worker beat` — look for `celery@… ready`.

### Smoke test (end-to-end sanity)

```bash
TOKEN=$(curl -s -X POST localhost:8000/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"test@jobpilot.dev","password":"changeme123"}' | jq -r .access_token)
curl -s -X POST localhost:8000/resumes -H "Authorization: Bearer $TOKEN" \
  -F "file=@resume.pdf;type=application/pdf" -F "consent=true"
curl -s -X POST localhost:8000/pipeline/run -H "Authorization: Bearer $TOKEN"     # -> {pipeline_id}
curl -s localhost:8000/pipeline/status -H "Authorization: Bearer $TOKEN"          # live stage/counts
curl -s localhost:8000/cost/report    -H "Authorization: Bearer $TOKEN"          # per-agent $ + savings
```

---

## 4. Migrations

- Applied by the `migrate` service (`alembic upgrade head`) on every `up`; idempotent.
- Manual: `docker compose run --rm migrate alembic upgrade head`.
- Current head: `0008_cv_drafts` (+ Phase-7 `0007` embeddings/HNSW). Check with
  `docker compose run --rm migrate alembic current`.
- **Forward-only in prod.** A downgrade that drops a column is destructive — prefer a new
  forward migration. Take a DB backup (below) before any migration on real data.

---

## 5. Scaling

- **`worker` (data plane):** scale horizontally — `docker compose up -d --scale worker=N`.
  Bounds spend via the code caps (`qualify_max_jobs`, crawler pages, batch size) + the
  per-user rate limits (8c) + the account Console cap.
- **`orchestrator-worker`:** can scale, but the **one-active-pipeline-per-user** DB guard
  keeps concurrency correct regardless.
- **`beat`:** **singleton only.**
- **`backend`:** stateless — scale behind a load balancer; point the LB healthcheck at
  `/health` and readiness at `/ready`.
- **Redis `visibility_timeout` = 6h** (> any pipeline) so a long task isn't spuriously
  redelivered; don't lower it below your longest pipeline.

---

## 6. Backup & restore

**Postgres (all app data + pgvector + LangGraph checkpoints):**
```bash
# backup
docker compose exec -T db pg_dump -U jobpilot -Fc jobpilot > backup/jobpilot_$(date +%F).dump
# restore (into a fresh/empty db)
docker compose exec -T db pg_restore -U jobpilot -d jobpilot --clean --if-exists < backup/jobpilot_YYYY-MM-DD.dump
```

**Object storage (resumes + rendered CVs, under `resumes/{user_id}/…`):**
```bash
# with the mc client against MinIO/S3
mc mirror local-minio/jobpilot-resumes  backup/storage/     # backup
mc mirror backup/storage/  local-minio/jobpilot-resumes     # restore
```

- Back up **both** Postgres and storage together — a `cv_drafts` row references a stored
  PDF/DOCX; a split restore leaves dangling refs.
- **GDPR:** `DELETE /me/data` (→ `delete_user_data`) hard-deletes a user's rows + storage
  prefix (verified zero-residue, Phase 8f). Backups also contain personal data — apply the
  same retention/erasure policy to backup media.

---

## 7. Rollback

1. **App code:** deploy is image-tag based — redeploy the previous tag
   (`docker compose pull && up -d`, or pin `image:` tags per service). Backend/frontend/
   workers are stateless, so a code rollback is safe on its own.
2. **Schema:** if the bad release shipped a migration, roll **code** back first; only
   `alembic downgrade` if the migration is reversible **and** you have a fresh backup —
   otherwise restore Postgres from the pre-migration dump (§6).
3. **Config/secret:** `.env` change → restart the affected service
   (`docker compose up -d <svc>`); no rebuild needed for runtime env.
4. **Rate-limit / tracing escape hatches:** `RATE_LIMIT_ENABLED=false` /
   `TRACING_ENABLED=false` disable those subsystems without a code change (both fail-open /
   no-op by design).

---

## 8. Operational reference

- **Cost:** `GET /cost/report` (per-user $ by agent/day, batch+cache savings, threshold
  alert). Watch `jobpilot.cost` warnings; tune `COST_ALERT_DAILY_USD`.
- **Rate limits:** `RATE_LIMIT_TRIGGER_PER_MIN` / `_CHAT_PER_MIN` / `_AUTH_PER_MIN`
  (fail-open on a Redis outage — never locks users out).
- **Tracing:** connected spans across agents/stages/MCP/LLM with token/cost, correlated by
  `pipeline_id`; no PII.
- **Stuck pipeline:** `reap_stale_pipelines` fails an umbrella stuck > `pipeline_stale_seconds`
  (1h) so a crashed worker never locks a user out permanently.
