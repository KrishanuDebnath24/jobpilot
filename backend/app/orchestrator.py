"""LangGraph orchestrator — the Phase 4 control plane.

Sequences the existing Celery stages (Scout → Qualify) **without a human between
them**. The heavy work stays in the Celery data-plane workers; this graph only
holds pipeline state + sequencing. Each node:

  1. triggers its stage (``celery.send_task`` onto the data-plane queue),
  2. awaits completion via the Redis event bus (fast path) **reconciled against**
     the persisted ``pipeline_runs`` status (source of truth — a dropped event
     can't hang the run),
  3. folds run-ids + counts into the envelope and advances.

State is checkpointed to Postgres with ``thread_id = pipeline_id``, so a crash
mid-pipeline **resumes** from the last completed node (it does not re-run Scout,
and does not double-trigger). No LLM runs here — it's deterministic control logic.

The graph is built to grow: Analyze/Enhance nodes append in Phase 5, approval
gates insert in Phase 6, additively.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
import uuid
from typing import TypedDict

from jobpilot_core import events, gates
from jobpilot_core.repositories import UMBRELLA_STAGE
from jobpilot_core.settings import Settings, get_settings
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from . import mcp_client
from .celery_app import celery

logger = logging.getLogger("jobpilot.orchestrator")

# psycopg async needs a selector loop; harmless on Linux (already selector), needed
# if a worker is ever run on Windows.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

_TERMINAL = ("completed", "failed")
_setup_done = False
_setup_lock = asyncio.Lock()


# --------------------------------------------------------------------------- #
# Graph state / envelope — lightweight references only, never job payloads.
# --------------------------------------------------------------------------- #
class PipelineState(TypedDict, total=False):
    user_id: str
    pipeline_id: str
    stage_run_ids: dict   # {"scout": run_id, "qualify": run_id}
    counts: dict          # {"scout": {...summary}, "qualify": {...summary}}
    status: str           # "running" | "failed"
    failed_stage: str | None
    error: str | None


async def _emit(user_id: uuid.UUID, event: dict) -> None:
    try:
        await events.publish_event(get_settings().redis_url, user_id, event)
        await events.beat(get_settings().redis_url, user_id, event.get("stage", "pipeline"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("orchestrator.emit failed: %s", exc)


def _psycopg_dsn(settings: Settings) -> str:
    """LangGraph's Postgres checkpointer uses psycopg (v3), not asyncpg."""
    return settings.database_url.replace("postgresql+asyncpg://", "postgresql://")


async def _stage_status(user_id: uuid.UUID, pipeline_id: str, stage: str) -> dict | None:
    res = await mcp_client.db_mcp(
        user_id, "get_stage_status", {"pipeline_id": pipeline_id, "stage": stage}
    )
    return res.get("run")


async def _await_stage(
    settings: Settings, user_id: uuid.UUID, pipeline_id: str, stage: str
) -> dict:
    """Block until the stage's persisted row is terminal. Events accelerate the
    wakeup; the DB is polled every ≤2s regardless, so a missed event only costs
    latency — never a hang."""
    deadline = time.monotonic() + settings.stage_timeout_seconds
    async with events.subscribe(settings.redis_url, user_id) as ps:
        gen = events.listen(ps, timeout=2.0)
        while time.monotonic() < deadline:
            # Waiting IS the orchestrator's work, and it emits nothing while it does it, so
            # the umbrella has to beat here or the reaper would read a healthy run as dead.
            await events.beat(settings.redis_url, user_id, UMBRELLA_STAGE)
            row = await _stage_status(user_id, pipeline_id, stage)
            if row and row["status"] in _TERMINAL:
                return row
            # Wait for the next event or a 2s tick, then loop back to the DB check
            # (the runner commits the terminal row *before* publishing, so an event
            # implies the row is already authoritative).
            try:
                await asyncio.wait_for(gen.__anext__(), timeout=2.5)
            except (asyncio.TimeoutError, StopAsyncIteration):
                pass
    raise TimeoutError(
        f"stage '{stage}' did not finish within {settings.stage_timeout_seconds}s"
    )


async def _run_stage_node(
    state: PipelineState, stage: str, task_name: str, extra_kwargs: dict | None = None
) -> dict:
    """Generic node: ensure the stage completes exactly once, then fold results.
    Resume-safe: if a prior (killed) invocation already finished or is mid-flight,
    we reconcile instead of re-triggering."""
    settings = get_settings()
    user_id = uuid.UUID(state["user_id"])
    pid = state["pipeline_id"]

    row = await _stage_status(user_id, pid, stage)
    if not (row and row["status"] in _TERMINAL):
        if not (row and row["status"] == "running"):
            # Not started yet -> trigger on the DATA-PLANE queue (never inline: a
            # long crawl must not block the control-plane worker). pipeline_id MUST go
            # as a kwarg: stage tasks have different positional signatures (qualify_run's
            # 2nd positional is requalify_all), so a positional pid would both mis-set
            # that and drop the correlation id. Analyze/Enhance also pass their scope.
            celery.send_task(task_name, args=[state["user_id"]],
                             kwargs={"pipeline_id": pid, **(extra_kwargs or {})},
                             queue=settings.data_queue)
            logger.info("orchestrator.triggered stage=%s pipeline=%s", stage, pid)
        row = await _await_stage(settings, user_id, pid, stage)

    counts = {**state.get("counts", {}), stage: (row.get("counts") if row else None)}
    run_ids = {**state.get("stage_run_ids", {}), stage: (row.get("id") if row else None)}
    if row and row["status"] == "failed":
        return {"counts": counts, "stage_run_ids": run_ids, "status": "failed",
                "failed_stage": stage, "error": row.get("error") or f"{stage} failed"}
    return {"counts": counts, "stage_run_ids": run_ids}


async def _scout_node(state: PipelineState) -> dict:
    return await _run_stage_node(state, "scout", "scout.run")


async def _qualify_node(state: PipelineState) -> dict:
    return await _run_stage_node(state, "qualify", "qualify.run")


# Phase 5: appended only when `analyze_in_pipeline` is on (auto-pipeline stops at
# Qualify by default). Both run the whole qualified set.
async def _analyze_node(state: PipelineState) -> dict:
    return await _run_stage_node(state, "analyze", "analyze.run", {"all_qualified": True})


async def _enhance_node(state: PipelineState) -> dict:
    return await _run_stage_node(state, "enhance", "enhance.run", {"all_qualified": True})


# --------------------------------------------------------------------------- #
# Gate nodes (Phase 6, 6a) — a configurable pause point after a stage. When the gate
# is on and unresolved it emits ``gate_pending`` and LangGraph-``interrupt``s, which
# checkpoints to Postgres and suspends until approve/skip. Reconciled against the
# persisted gate row so a resumed/replayed pipeline never re-pauses a cleared gate and
# never hangs on an approval event (same discipline as the stage nodes).
# --------------------------------------------------------------------------- #
async def _gate_status(user_id: uuid.UUID, pipeline_id: str, gate_key: str) -> str | None:
    res = await mcp_client.db_mcp(
        user_id, "get_stage_status",
        {"pipeline_id": pipeline_id, "stage": gates.gate_stage(gate_key)})
    row = res.get("run")
    return row["status"] if row else None


async def _run_gate(state: PipelineState, gate_key: str) -> dict:
    user_id = uuid.UUID(state["user_id"])
    pid = state["pipeline_id"]
    try:
        cfg = await mcp_client.config_mcp(user_id, "get_gate_config", {})
        enabled = bool((cfg.get("gates") or {}).get(gate_key))
    except Exception as exc:  # noqa: BLE001 - a config read failure must not wedge a run
        logger.warning("gate.config read failed (%s) — treating %s as off", exc, gate_key)
        enabled = False
    status = await _gate_status(user_id, pid, gate_key)
    if gates.gate_decision(enabled, status) == "proceed":
        return {}
    # Pause: persist pending (idempotent), emit, then interrupt (checkpoints + suspends).
    await mcp_client.db_mcp(user_id, "open_gate_pending", {"pipeline_id": pid, "gate_key": gate_key})
    gid = gates.gate_id(pid, gate_key)
    await _emit(user_id, events.make_event(
        "gate", "pending", pipeline_id=pid, gate_key=gate_key, gate_id=gid))
    logger.info("orchestrator.gate paused pipeline=%s gate=%s", pid, gate_key)
    interrupt({"gate_key": gate_key, "gate_id": gid})  # suspends here until resumed
    return {}


def _make_gate_node(gate_key: str):
    async def _node(state: PipelineState) -> dict:
        return await _run_gate(state, gate_key)

    return _node


async def _failed_node(state: PipelineState) -> dict:
    # Terminal marker. The umbrella row + failed event are finalized by the wrapper.
    logger.warning("orchestrator.failed stage=%s error=%s",
                   state.get("failed_stage"), state.get("error"))
    return {}


def _route(state: PipelineState) -> str:
    return "failed" if state.get("status") == "failed" else "ok"


def build_graph(checkpointer) -> object:
    """[scout] → (gate) → [qualify] → (gate) → END, with a conditional error edge to
    [failed]. Scout failure must NOT let Qualify run on no data. A gate node sits after
    each stage: it no-ops when the gate is off (default) and interrupts when on (Phase
    6). When ``analyze_in_pipeline`` is on (Phase 5, default OFF), [analyze] → [enhance]
    (each gated) are appended after qualify — otherwise the auto-pipeline stops at
    Qualify."""
    g = StateGraph(PipelineState)
    g.add_node("scout", _scout_node)
    g.add_node("qualify", _qualify_node)
    g.add_node("gate_scout", _make_gate_node("after_scout"))
    g.add_node("gate_qualify", _make_gate_node("after_qualify"))
    g.add_node("failed", _failed_node)
    g.add_edge(START, "scout")
    g.add_conditional_edges("scout", _route, {"ok": "gate_scout", "failed": "failed"})
    g.add_edge("gate_scout", "qualify")
    if get_settings().analyze_in_pipeline:
        g.add_node("analyze", _analyze_node)
        g.add_node("enhance", _enhance_node)
        g.add_node("gate_analyze", _make_gate_node("after_analyze"))
        g.add_conditional_edges("qualify", _route, {"ok": "gate_qualify", "failed": "failed"})
        g.add_edge("gate_qualify", "analyze")
        g.add_conditional_edges("analyze", _route, {"ok": "gate_analyze", "failed": "failed"})
        g.add_edge("gate_analyze", "enhance")
        g.add_conditional_edges("enhance", _route, {"ok": END, "failed": "failed"})
    else:
        g.add_conditional_edges("qualify", _route, {"ok": "gate_qualify", "failed": "failed"})
        g.add_edge("gate_qualify", END)
    g.add_edge("failed", END)
    return g.compile(checkpointer=checkpointer)


async def _ensure_setup(cp: AsyncPostgresSaver) -> None:
    global _setup_done
    if _setup_done:
        return
    async with _setup_lock:
        if not _setup_done:
            await cp.setup()  # idempotent: creates checkpoint tables if absent
            _setup_done = True


def _summary(state: PipelineState) -> dict:
    status = "failed" if state.get("status") == "failed" else "completed"
    counts = state.get("counts") or {}
    # Only include stages that actually ran (analyze/enhance are config-gated).
    stage_counts = {k: counts[k] for k in ("scout", "qualify", "analyze", "enhance") if k in counts}
    return {
        "pipeline_id": state.get("pipeline_id"),
        "status": status,
        "counts": stage_counts or {"scout": counts.get("scout"), "qualify": counts.get("qualify")},
        "error": state.get("error"),
    }


def _is_interrupted(snap) -> bool:
    """True if the graph is paused at a gate interrupt (as opposed to a normal
    pending step). Checks both the top-level ``interrupts`` and per-task interrupts,
    across langgraph versions."""
    if getattr(snap, "interrupts", None):
        return True
    return any(getattr(t, "interrupts", None) for t in (getattr(snap, "tasks", ()) or ()))


async def run_pipeline_graph(user_id: str, pipeline_id: str) -> dict:
    """Execute, RESUME, or CONTINUE-PAST-A-GATE the pipeline graph for one execution.
    Idempotent per ``pipeline_id``: a redelivered task resumes from the checkpoint
    rather than re-running completed stages, and an approve/skip re-enqueues this same
    task to continue past the gate (the resolved gate row drives it forward)."""
    settings = get_settings()
    uid = uuid.UUID(user_id)

    async with AsyncPostgresSaver.from_conn_string(_psycopg_dsn(settings)) as cp:
        await _ensure_setup(cp)
        graph = build_graph(cp)
        cfg = {"configurable": {"thread_id": pipeline_id}}
        snap = await graph.aget_state(cfg)
        if _is_interrupted(snap):  # paused at a gate -> resume past it
            logger.info("orchestrator.resume-gate pipeline=%s", pipeline_id)
            final = await graph.ainvoke(Command(resume={}), cfg)
        elif snap.next:  # a step is pending -> RESUME (do not restart)
            logger.info("orchestrator.resume pipeline=%s next=%s", pipeline_id, snap.next)
            final = await graph.ainvoke(None, cfg)
        elif snap.values:  # already ran to completion for this thread -> no-op
            logger.info("orchestrator.replay pipeline=%s already complete", pipeline_id)
            final = snap.values
        else:  # fresh start
            await _emit(uid, events.make_event("pipeline", events.STARTED, pipeline_id=pipeline_id))
            init: PipelineState = {
                "user_id": user_id, "pipeline_id": pipeline_id,
                "status": "running", "counts": {}, "stage_run_ids": {},
            }
            final = await graph.ainvoke(init, cfg)
        # Did we stop at a (new) gate interrupt rather than reaching the end?
        paused = _is_interrupted(await graph.aget_state(cfg))

    if paused:
        logger.info("orchestrator.paused pipeline=%s (awaiting gate approval)", pipeline_id)
        return {"pipeline_id": pipeline_id, "status": "paused"}

    summary = _summary(final)
    # Finalize the umbrella row (created by pipeline-mcp's guarded open) + emit.
    umb = await _stage_status(uid, pipeline_id, "pipeline")
    if umb and umb["status"] not in _TERMINAL:
        await mcp_client.db_mcp(uid, "finish_pipeline_run", {
            "run_id": umb["id"], "status": summary["status"],
            "counts": summary["counts"], "error": summary["error"]})
    kind = events.FAILED if summary["status"] == "failed" else events.COMPLETED
    await _emit(uid, events.make_event(
        "pipeline", kind, pipeline_id=pipeline_id,
        counts=summary["counts"], error=summary["error"]))
    logger.info("orchestrator.done pipeline=%s status=%s", pipeline_id, summary["status"])
    return summary
