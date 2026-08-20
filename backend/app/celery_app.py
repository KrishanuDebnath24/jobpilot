"""Celery app — Scout runs as a background job (Redis broker + result backend).

Manual trigger for now (``POST /scout/run``); automatic orchestration is Phase 4.
Set ``CELERY_TASK_ALWAYS_EAGER=1`` to run tasks inline (used by tests).
"""

from __future__ import annotations

import os

from celery import Celery
from celery.signals import worker_process_init
from jobpilot_core import tracing
from jobpilot_core.settings import get_settings

settings = get_settings()


@worker_process_init.connect
def _init_worker_observability(**_kwargs) -> None:
    """Phase 8a: init correlation logging + tracing per prefork child (so a
    BatchSpanProcessor's exporter thread lives in the child, not across the fork)."""
    tracing.configure_logging()
    tracing.init_tracing(settings, service_name="jobpilot-worker")

celery = Celery(
    "jobpilot",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.tasks"],  # explicit include avoids importing tasks at module load
)
celery.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    broker_connection_retry_on_startup=True,
    # Redis broker restores an unacked (crashed) task after this window — so it must
    # exceed the longest task, or a still-running orchestrator/stage gets redelivered
    # and double-runs. Gap-close waits out its analyze batch with no deadline of its own,
    # and Anthropic only expires a batch at 24h, so the ceiling on a legitimate task is now
    # ~24h rather than the ~6h a pipeline used to need. 26h keeps redelivery strictly a
    # crash-recovery mechanism; a shorter window would start a second, duplicate-spend run
    # on top of one that is merely still waiting.
    broker_transport_options={"visibility_timeout": 26 * 3600},
    # Control plane / data plane split: the orchestrator waits on stages, so it must
    # not share a worker pool with them (a waiting orchestrator would starve the
    # stage it's waiting for). Route it to its own queue, consumed by a dedicated
    # orchestrator worker. Stages + the scheduler tick stay on the default queue.
    task_routes={"pipeline.run_full": {"queue": settings.orchestrator_queue}},
    # Beat: check for due scheduled pipelines every tick.
    beat_schedule={
        "pipeline-scheduler-tick": {
            "task": "pipeline.scheduler_tick",
            "schedule": float(settings.beat_tick_seconds),
        }
    },
)

if os.environ.get("CELERY_TASK_ALWAYS_EAGER") == "1":
    celery.conf.task_always_eager = True
    celery.conf.task_eager_propagates = True
