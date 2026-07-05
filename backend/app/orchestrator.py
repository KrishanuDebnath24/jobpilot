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

from jobpilot_core import events
from jobpilot_core.settings import Settings, get_settings
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph

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


async def _run_stage_node(state: PipelineState, stage: str, task_name: str) -> dict:
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
            # as a kwarg: scout_run/qualify_run have different positional signatures
            # (qualify_run's 2nd positional is requalify_all), so a positional pid
            # would both mis-set requalify_all and drop the correlation id.
            celery.send_task(task_name, args=[state["user_id"]],
                             kwargs={"pipeline_id": pid}, queue=settings.data_queue)
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


async def _failed_node(state: PipelineState) -> dict:
    # Terminal marker. The umbrella row + failed event are finalized by the wrapper.
    logger.warning("orchestrator.failed stage=%s error=%s",
                   state.get("failed_stage"), state.get("error"))
    return {}


def _route(state: PipelineState) -> str:
    return "failed" if state.get("status") == "failed" else "ok"


def build_graph(checkpointer) -> object:
    """[scout] → [qualify] → END, with a conditional error edge to [failed].
    Scout failure must NOT let Qualify run on no data."""
    g = StateGraph(PipelineState)
    g.add_node("scout", _scout_node)
    g.add_node("qualify", _qualify_node)
    g.add_node("failed", _failed_node)
    g.add_edge(START, "scout")
    g.add_conditional_edges("scout", _route, {"ok": "qualify", "failed": "failed"})
    g.add_conditional_edges("qualify", _route, {"ok": END, "failed": "failed"})
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
    return {
        "pipeline_id": state.get("pipeline_id"),
        "status": status,
        "counts": {"scout": counts.get("scout"), "qualify": counts.get("qualify")},
        "error": state.get("error"),
    }


async def run_pipeline_graph(user_id: str, pipeline_id: str) -> dict:
    """Execute (or RESUME) the pipeline graph for one execution. Idempotent per
    ``pipeline_id``: a redelivered task resumes from the checkpoint rather than
    re-running completed stages."""
    settings = get_settings()
    uid = uuid.UUID(user_id)
    await _emit(uid, events.make_event("pipeline", events.STARTED, pipeline_id=pipeline_id))

    async with AsyncPostgresSaver.from_conn_string(_psycopg_dsn(settings)) as cp:
        await _ensure_setup(cp)
        graph = build_graph(cp)
        cfg = {"configurable": {"thread_id": pipeline_id}}
        snap = await graph.aget_state(cfg)
        if snap.next:  # a step is pending -> RESUME (do not restart)
            logger.info("orchestrator.resume pipeline=%s next=%s", pipeline_id, snap.next)
            final = await graph.ainvoke(None, cfg)
        elif snap.values:  # already ran to completion for this thread -> no-op
            logger.info("orchestrator.replay pipeline=%s already complete", pipeline_id)
            final = snap.values
        else:  # fresh start
            init: PipelineState = {
                "user_id": user_id, "pipeline_id": pipeline_id,
                "status": "running", "counts": {}, "stage_run_ids": {},
            }
            final = await graph.ainvoke(init, cfg)

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
