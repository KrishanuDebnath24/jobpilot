"""pipeline-mcp — the Phase 4 control surface (streamable MCP).

Same security core as the other MCP services: ``user_id`` comes ONLY from the
signed request context. A user can only trigger/see **their own** pipeline; the
concurrency guard, status, and history are all user-scoped by the runtime.

Owns the pipeline/umbrella lifecycle + control tools; db-mcp still tracks the
individual stage runs from the worker. Both go through the same ``repositories``
layer (one source of query logic), so co-ownership of ``pipeline_runs`` is safe.

This service does NOT run the graph — it enqueues the orchestrator task (by name,
onto the orchestrator queue) and reads status. The heavy work is elsewhere.
"""

from __future__ import annotations

import uuid

from celery import Celery
from jobpilot_core import events, gates, repositories
from jobpilot_core.db import session_scope
from jobpilot_core.mcp_auth import UserContextMiddleware, require_user_id
from jobpilot_core.settings import get_settings
from mcp.server.fastmcp import FastMCP
from sqlalchemy.exc import IntegrityError

settings = get_settings()
mcp = FastMCP("pipeline-mcp", stateless_http=True, json_response=True, host="0.0.0.0", port=8005)

# A producer-only Celery client: we only ever enqueue by task name (the tasks live
# in the backend/worker image). No task code is imported here.
_celery = Celery("jobpilot-pipeline-mcp", broker=settings.redis_url, backend=settings.redis_url)
_celery.conf.update(task_serializer="json", accept_content=["json"], result_serializer="json")


async def _reap(uid) -> int:
    """Close any run row whose worker has stopped beating, for this user.

    Called on every read as well as every trigger, so the UI heals itself. Before this, a
    stage row orphaned by a lost worker was never reaped at all — it sat at 'running'
    forever — and the umbrella needed an hour AND a fresh trigger to clear, which locked
    the user out of starting a new pipeline in the meantime.

    Never raises: a reap failure must degrade to "the old status" and never break status."""
    try:
        async with session_scope() as session:
            stages = await repositories.active_run_stages(session, uid)
            if not stages:
                return 0
            alive = await events.alive_stages(
                settings.redis_url, uid, stages, settings.run_stale_seconds)
            return await repositories.reap_stale_runs(
                session, uid, alive, settings.run_grace_seconds)
    except Exception:  # noqa: BLE001 - status must survive a failed reap
        return 0


@mcp.tool()
async def run_full_pipeline() -> dict:
    """Kick off an autonomous Scout→Qualify run. Enforces **one active pipeline per
    user**: a second trigger while one is running is REJECTED (not queued) with the
    active pipeline_id. Stale umbrellas (crashed worker) are reaped first so a user
    is never locked out permanently."""
    uid = require_user_id()
    pid = uuid.uuid4()

    await _reap(uid)
    async with session_scope() as session:
        # Age-only backstop, for an umbrella opened before heartbeating existed: its stage
        # will never beat, so nothing but age can ever release it.
        await repositories.reap_stale_pipelines(session, uid, settings.pipeline_stale_seconds)

    try:
        async with session_scope() as session:
            await repositories.open_pipeline(session, uid, pid)  # partial-unique guard
    except IntegrityError:
        async with session_scope() as session:
            active = await repositories.get_active_pipeline(session, uid)
        return {
            "rejected": True,
            "reason": "a pipeline is already running for this user",
            "active_pipeline_id": str(active.pipeline_id) if active and active.pipeline_id else None,
        }

    _celery.send_task("pipeline.run_full", args=[str(uid), str(pid)],
                      queue=settings.orchestrator_queue)
    return {"pipeline_id": str(pid), "status": "queued"}


def _pending_gate(pipeline: dict | None) -> dict | None:
    """Surface a paused gate (if any) so the UI/Concierge can prompt for approval."""
    if not pipeline:
        return None
    for stage, run in (pipeline.get("stages") or {}).items():
        if stage.startswith(gates.GATE_STAGE_PREFIX) and run and run.get("status") == "pending":
            gate_key = stage[len(gates.GATE_STAGE_PREFIX):]
            return {"gate_key": gate_key, "gate_id": gates.gate_id(pipeline["pipeline_id"], gate_key)}
    return None


@mcp.tool()
async def get_pipeline_status() -> dict:
    """Current stage + per-stage counts for the active run, or the most recent one.
    Includes ``pending_gate`` when the pipeline is paused awaiting a review approval.

    Reaps dead runs first: this is the endpoint the live UI polls, so healing here is what
    turns "a spinner for a task that no longer exists" into a failed run you can retry."""
    uid = require_user_id()
    await _reap(uid)
    async with session_scope() as session:
        active = await repositories.get_active_pipeline(session, uid)
        if active and active.pipeline_id:
            pipeline = await repositories.get_pipeline(session, uid, active.pipeline_id)
            return {"pipeline": pipeline, "active": True, "pending_gate": _pending_gate(pipeline)}
        recent = await repositories.list_pipelines(session, uid, limit=1)
        pipeline = recent[0] if recent else None
        return {"pipeline": pipeline, "active": False, "pending_gate": _pending_gate(pipeline)}


@mcp.tool()
async def approve_gate(gate_id: str) -> dict:
    """Approve a pending review gate → resume the paused pipeline. Records the decision
    against the persisted gate row (source of truth) FIRST, then re-enqueues the
    orchestrator task, which reconciles and continues past the gate."""
    return await _resolve_gate(gate_id, "approve")


@mcp.tool()
async def skip_gate(gate_id: str) -> dict:
    """Skip a pending review gate → resume the paused pipeline without the gated review.
    Same resume mechanism as approve_gate; both advance the graph from the checkpoint."""
    return await _resolve_gate(gate_id, "skip")


async def _resolve_gate(gate_id: str, decision: str) -> dict:
    uid = require_user_id()
    try:
        pipeline_id, gate_key = gates.parse_gate_id(gate_id)
    except ValueError as exc:
        return {"error": str(exc)}
    async with session_scope() as session:
        await repositories.resolve_gate(session, uid, uuid.UUID(pipeline_id), gate_key, decision)
    # Re-enqueue the SAME orchestrator task; it resumes from the Postgres checkpoint and
    # the now-resolved gate row lets the gate node proceed (no hang on an event).
    _celery.send_task("pipeline.run_full", args=[str(uid), pipeline_id],
                      queue=settings.orchestrator_queue)
    return {"gate_id": gate_id, "decision": "approved" if decision == "approve" else "skipped",
            "status": "resuming"}


@mcp.tool()
async def trigger_stage(
    stage: str,
    job_ids: list[str] | None = None,
    all_qualified: bool = False,
) -> dict:
    """Run one stage standalone. ``scout`` | ``qualify`` (whole pool), or the Phase-5
    ``analyze`` | ``enhance`` on a scope (``job_ids`` or ``all_qualified``). Rejected
    while a full pipeline is active (it already owns the job pool — no racing on it)."""
    uid = require_user_id()
    if stage not in ("scout", "qualify", "analyze", "enhance"):
        return {"error": f"unknown stage {stage!r} (expected scout|qualify|analyze|enhance)"}
    async with session_scope() as session:
        active = await repositories.get_active_pipeline(session, uid)
    if active:
        return {"rejected": True, "reason": "a pipeline is already running",
                "active_pipeline_id": str(active.pipeline_id) if active.pipeline_id else None}
    pid = str(uuid.uuid4())
    # pipeline_id as a kwarg (stage tasks have different positional shapes). Analyze/
    # Enhance also carry their scope as kwargs.
    kwargs: dict = {"pipeline_id": pid}
    if stage in ("analyze", "enhance"):
        kwargs["job_ids"] = job_ids
        kwargs["all_qualified"] = all_qualified
    _celery.send_task(f"{stage}.run", args=[str(uid)], kwargs=kwargs, queue=settings.data_queue)
    return {"stage": stage, "pipeline_id": pid, "status": "queued"}


@mcp.tool()
async def list_runs(limit: int = 20) -> dict:
    """Recent pipeline executions (grouped by pipeline_id), newest first."""
    uid = require_user_id()
    await _reap(uid)
    async with session_scope() as session:
        return {"runs": await repositories.list_pipelines(session, uid, limit=limit)}


@mcp.tool()
async def get_run(pipeline_id: str) -> dict:
    """One execution: umbrella status + its per-stage runs."""
    uid = require_user_id()
    async with session_scope() as session:
        return {"run": await repositories.get_pipeline(session, uid, uuid.UUID(pipeline_id))}


app = UserContextMiddleware(mcp.streamable_http_app(), settings)
