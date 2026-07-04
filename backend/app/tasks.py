"""Celery tasks. Scout's long-running fan-out runs here, off the request path."""

from __future__ import annotations

import asyncio
import uuid

from .celery_app import celery
from .qualify_runner import run_qualify_tracked
from .scout_runner import run_scout_tracked


@celery.task(name="scout.run")
def scout_run(user_id: str) -> dict:
    """Background Scout: fan out connectors -> normalize/dedupe -> persist via
    db-mcp, recording a pipeline_runs row. Returns the run summary."""
    return asyncio.run(run_scout_tracked(uuid.UUID(user_id)))


@celery.task(name="qualify.run")
def qualify_run(user_id: str, requalify_all: bool = False) -> dict:
    """Background Qualify: hybrid-score the job pool (deterministic gate + gated
    Haiku refine), persist qualifications via db-mcp, record a pipeline_runs row."""
    return asyncio.run(run_qualify_tracked(uuid.UUID(user_id), requalify_all=requalify_all))
