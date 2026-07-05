"""Celery tasks. Stage fan-outs (Scout/Qualify) run on the default data-plane
queue; the LangGraph orchestrator (``pipeline.run_full``) runs on its own
``orchestrator`` queue so awaiting a stage never blocks a stage from running."""

from __future__ import annotations

import uuid

from .asyncrun import run_async
from .celery_app import celery
from .orchestrator import run_pipeline_graph
from .qualify_runner import run_qualify_tracked
from .scout_runner import run_scout_tracked


@celery.task(name="scout.run")
def scout_run(user_id: str, pipeline_id: str | None = None) -> dict:
    """Background Scout: fan out connectors -> normalize/dedupe -> persist via
    db-mcp, recording a pipeline_runs row correlated under pipeline_id. Returns the
    run summary."""
    return run_async(run_scout_tracked(uuid.UUID(user_id), pipeline_id=pipeline_id))


@celery.task(name="qualify.run")
def qualify_run(user_id: str, requalify_all: bool = False, pipeline_id: str | None = None) -> dict:
    """Background Qualify: hybrid-score the job pool (deterministic gate + gated
    Haiku refine), persist qualifications via db-mcp, record a pipeline_runs row."""
    return run_async(
        run_qualify_tracked(uuid.UUID(user_id), requalify_all=requalify_all, pipeline_id=pipeline_id)
    )


# --------------------------------------------------------------------------- #
# Orchestrator — the durable control-plane task (LangGraph). acks_late +
# reject_on_worker_lost so a task killed mid-pipeline is redelivered and RESUMES
# from its Postgres checkpoint (rather than being lost).
# --------------------------------------------------------------------------- #
@celery.task(name="pipeline.run_full", acks_late=True, reject_on_worker_lost=True)
def run_full_pipeline(user_id: str, pipeline_id: str) -> dict:
    """Run the autonomous Scout→Qualify graph for one execution. Returns immediately
    to the caller (it's enqueued); executes here in the orchestrator worker."""
    return run_async(run_pipeline_graph(user_id, pipeline_id))


@celery.task(name="pipeline.scheduler_tick")
def scheduler_tick() -> dict:
    """Beat-driven: fire a full pipeline for every user whose schedule is due,
    respecting the one-active-pipeline guard. Runs on the data-plane worker."""
    from .scheduler import run_scheduler_tick

    return run_async(run_scheduler_tick())
