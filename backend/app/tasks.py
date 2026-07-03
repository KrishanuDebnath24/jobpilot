"""Celery tasks. Scout's long-running fan-out runs here, off the request path."""

from __future__ import annotations

import asyncio
import uuid

from .celery_app import celery
from .scout_runner import run_scout_tracked


@celery.task(name="scout.run")
def scout_run(user_id: str) -> dict:
    """Background Scout: fan out connectors -> normalize/dedupe -> persist via
    db-mcp, recording a pipeline_runs row. Returns the run summary."""
    return asyncio.run(run_scout_tracked(uuid.UUID(user_id)))
