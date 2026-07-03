"""Celery app — Scout runs as a background job (Redis broker + result backend).

Manual trigger for now (``POST /scout/run``); automatic orchestration is Phase 4.
Set ``CELERY_TASK_ALWAYS_EAGER=1`` to run tasks inline (used by tests).
"""

from __future__ import annotations

import os

from celery import Celery
from jobpilot_core.settings import get_settings

settings = get_settings()

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
)

if os.environ.get("CELERY_TASK_ALWAYS_EAGER") == "1":
    celery.conf.task_always_eager = True
    celery.conf.task_eager_propagates = True
