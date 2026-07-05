# Phase 4 Work Order — Orchestrator (Autonomous Scout→Qualify) + Live Status

> Hand this to Claude Code. It expands Phase 4 of the master prompt. Same discipline: sub-steps **in order**, reproduce each **show me** against live output, **stop at the gate**, log choices in `DECISIONS.md`, commit at the gate.
>
> **Depends on:** Phase 3 done (Scout and Qualify both exist as tracked Celery stages writing `pipeline_runs` rows). This phase makes them run **without a human between them.**
>
> **Cost note:** the orchestrator is a **state machine, not an LLM agent** — Phase 4 adds **zero new LLM cost**. It only sequences the existing paid stages (Scout's crawler extraction, Qualify's Haiku refinement). If you find yourself adding a model call to the orchestrator, stop — that's not its job.

---

## Decisions locked for this phase (don't re-litigate)

- **LangGraph is the control plane; Celery is the data plane.** The heavy work (crawls, LLM calls) stays in the existing Celery workers. LangGraph holds the *pipeline state and sequencing* — each graph node **triggers a stage and awaits its completion**, then advances. Don't move stage execution into graph nodes (a long crawl would block the graph); don't rebuild sequencing in Celery (you'd lose checkpointing + the Phase 6 gates).
- **The full pipeline runs as a durable Celery task.** `run_full_pipeline` enqueues an orchestrator task that executes the LangGraph graph, so the API returns immediately and the run survives restarts. Non-blocking, durable.
- **Checkpointing on Postgres.** Use LangGraph's Postgres checkpointer; **`thread_id` = the pipeline execution id**. This gives resumability (a crash mid-pipeline resumes, doesn't restart or double-run) and is the exact mechanism Phase 6's human gates will pause/resume on. Persist `user_id` in the graph state.
- **Correlate stage runs under one execution.** Add a `pipeline_id` (= the LangGraph `thread_id`) to `pipeline_runs` so the stages of one auto-run group together and the UI can show "Run X: Scout ✓ → Qualify ⏳".
- **Progression is event-driven but status-reconciled.** Stages emit completion events on the Redis bus (fast path), but a node must also be able to **confirm completion by reading persisted `pipeline_runs` status** (fallback). Pub/sub is fire-and-forget — never let a missed event hang a pipeline. Events advance; persisted status is the source of truth.
- **One active pipeline per user.** A concurrency guard rejects (or queues) a second trigger while one is running. No double-runs, no races on the same job pool.
- **Lightweight envelope, not blobs.** Nodes pass run ids, counts, and `user_id` forward — never full job payloads. Data lives in the DB; the graph moves references.
- **Boundary + scoping hold.** `pipeline-mcp` tools are user-scoped via the signed context; the graph's `user_id` is bound at pipeline start **from the authenticated session, never from any content**. A user can only trigger/see their own pipeline; checkpoints are user-scoped.
- **Design for extension.** The graph will grow (Analyze/Enhance nodes in Phase 5; approval gates in Phase 6). Keep nodes modular and edges explicit so appending stages is additive, not a rewrite.

---

## Sub-steps (each ends with a "show me")

### 4a — Pipeline execution model + correlation
- Add `pipeline_id` to `pipeline_runs` (migration). Define the typed graph **state / envelope**: `{ user_id, pipeline_id, stage_run_ids, counts, status, error? }`.
- **Show me:** a migration adds `pipeline_id`; a manual insert of two stage runs sharing a `pipeline_id` groups correctly when queried.

### 4b — Event bus (Redis pub/sub) + reconciliation
- Give Scout (2g) and Qualify (3e) an emit step: on finish/fail they publish `scout.completed` / `qualify.completed` / `<stage>.failed` with `pipeline_id` + counts. Build a subscribe utility **and** a `get_stage_status(pipeline_id, stage)` reader against `pipeline_runs`.
- **Show me:** triggering Scout publishes `scout.completed` with counts; a subscriber receives it; and `get_stage_status` returns the same completion independently of the event (proving the fallback works).

### 4c — LangGraph orchestrator (the control plane)
- Build a `StateGraph`: `[scout] → [qualify] → [end]`, with a conditional error edge to a terminal `failed` state. Each node: trigger the existing Celery stage → await completion via the bus, reconciling against `get_stage_status` → advance with the envelope. Scout failure routes to `failed` (Qualify must **not** run on no data); a connector *partial* failure (recorded in 2g) still counts as stage success and proceeds. Postgres checkpointer; `thread_id = pipeline_id`. **No LLM in the graph.**
- **Show me:** starting the graph for a user runs Scout, waits for it to finish, then **automatically** runs Qualify with no manual trigger in between, and lands qualified/disqualified results — reproduced end to end. Kill the process mid-run and restart: it **resumes** from the checkpoint rather than re-running Scout.

### 4d — `pipeline-mcp` (control tools) + concurrency guard
- Stand up `pipeline-mcp` (streamable-HTTP, add to Compose). Tools, all user-scoped: `run_full_pipeline` (enqueues the orchestrator task), `get_pipeline_status` (current stage + per-stage counts for the active/last run), `trigger_stage(stage)` (run one stage standalone), `list_runs`, `get_run(pipeline_id)`. Enforce **one active pipeline per user**.
- **Show me:** `run_full_pipeline` via `pipeline-mcp` kicks off an autonomous Scout→Qualify run; `get_pipeline_status` reflects live progress; a second concurrent `run_full_pipeline` is rejected/queued; user B cannot see or trigger user A's pipeline.

### 4e — Scheduled kickoff (so the user truly doesn't run it)
- Celery beat reads `search_configs.schedule` and calls `run_full_pipeline` on cadence per user, respecting the concurrency guard. This is the "agents run themselves" payoff.
- **Show me:** a due schedule fires an autonomous full pipeline with no human action; an already-running user's schedule tick is skipped, not stacked.

### 4f — Live status in the UI
- Backend SSE (or WebSocket) endpoint that relays this user's pipeline events from Redis to the browser. Frontend: a live pipeline view extending the Phase 3 dashboard — current stage, per-stage status + counts (Scout: done, 101 jobs; Qualify: running, 40/101), and completion. On load/refresh, fetch current state via `get_pipeline_status`, then subscribe to the live stream (handle reconnects).
- **Show me:** watching the dashboard *during* a run, Scout→Qualify progress updates **live without refresh**; on completion the qualified/disqualified table populates; refreshing mid-run re-syncs status and keeps streaming.

---

## Acceptance gate

Green when **all** hold, then stop for approval before Phase 5:
1. **The master bar:** an end-to-end run you didn't hand-hold — one trigger yields Scout→Qualify with no manual step between stages.
2. LangGraph orchestrates as the control plane with **Postgres checkpointing**; killing and restarting mid-run **resumes** (no re-run, no double-trigger).
3. Progression is event-driven **and** reconciled against persisted `pipeline_runs` status — a dropped event can't hang the pipeline.
4. `pipeline-mcp` controls the pipeline, user-scoped, with a **one-active-pipeline-per-user** guard; cross-user trigger/visibility is impossible.
5. **Failure routing** works: Scout failure stops the chain and surfaces an error with retry; a connector partial-failure still proceeds.
6. **Scheduled kickoff** runs a full pipeline with no human action, respecting concurrency.
7. The UI shows **live** stage progress and re-syncs on refresh.
8. Phase 4 adds **no new LLM cost** (orchestrator is a state machine); runs remain idempotent.

---

## Watch-outs

- **Never rely on pub/sub alone for state transitions.** Redis pub/sub drops messages if no subscriber is listening at that instant. Every critical "did the stage finish?" decision must be confirmable from persisted `pipeline_runs`. Events are the fast path; the DB is the truth.
- **Guard concurrency hard.** Two pipelines on one user's pool race on the same jobs/qualifications. One active run per user; second trigger rejected or queued — decide which and document it.
- **Distinguish stage-failure from partial-failure.** Scout returning zero because every connector died is a failure (don't qualify nothing). Scout returning jobs with one connector erroring is success-with-warnings — proceed. The Phase 2g error isolation already draws this line; the orchestrator must honor it.
- **Resumability is a feature, not an accident.** Checkpoint after each node so a crash/restart resumes cleanly. Test the kill-and-resume path explicitly — it's also the foundation Phase 6 gates pause on.
- **Keep the orchestrator LLM-free.** It's deterministic control logic. The Concierge chat agent that *drives* this graph is Phase 6 and is a separate component.
- **Build the graph to grow.** Analyze/Enhance nodes append in Phase 5; approval gates insert in Phase 6. Modular nodes + explicit edges now = additive later.
- **Security:** `user_id` bound at start from the session (never content-derived); graph state, checkpoints, and `pipeline-mcp` tools all user-scoped; a user can't reach another user's run.
- **Live-UI robustness:** relay per-user events only (don't leak another user's status onto the stream); handle browser reconnects by re-fetching status then resuming the stream.
- **New Compose service** (`pipeline-mcp`) — bring the stack up and confirm it's healthy and reachable, same as prior phases.
- **Commit at the gate**; log decisions (control-plane/data-plane split, checkpointer + `thread_id`=`pipeline_id`, concurrency policy, event-vs-poll reconciliation, schedule handling).
