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
from jobpilot_core import repositories
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


@mcp.tool()
async def run_full_pipeline() -> dict:
    """Kick off an autonomous Scout→Qualify run. Enforces **one active pipeline per
    user**: a second trigger while one is running is REJECTED (not queued) with the
    active pipeline_id. Stale umbrellas (crashed worker) are reaped first so a user
    is never locked out permanently."""
    uid = require_user_id()
    pid = uuid.uuid4()

    async with session_scope() as session:
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


@mcp.tool()
async def get_pipeline_status() -> dict:
    """Current stage + per-stage counts for the active run, or the most recent one."""
    uid = require_user_id()
    async with session_scope() as session:
        active = await repositories.get_active_pipeline(session, uid)
        if active and active.pipeline_id:
            return {"pipeline": await repositories.get_pipeline(session, uid, active.pipeline_id),
                    "active": True}
        recent = await repositories.list_pipelines(session, uid, limit=1)
        return {"pipeline": recent[0] if recent else None, "active": False}


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
    async with session_scope() as session:
        return {"runs": await repositories.list_pipelines(session, uid, limit=limit)}


@mcp.tool()
async def get_run(pipeline_id: str) -> dict:
    """One execution: umbrella status + its per-stage runs."""
    uid = require_user_id()
    async with session_scope() as session:
        return {"run": await repositories.get_pipeline(session, uid, uuid.UUID(pipeline_id))}


app = UserContextMiddleware(mcp.streamable_http_app(), settings)
