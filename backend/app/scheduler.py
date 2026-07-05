"""Scheduled kickoff — the "agents run themselves" payoff.

Celery beat ticks this every ``beat_tick_seconds``. For each user whose
``search_configs.schedule`` is due (cadence keyword → interval since their last
run), it fires a full pipeline **respecting the one-active-pipeline guard** — an
already-running user's tick is skipped, never stacked.

This is the one place that reads across users (the scheduler acts as the *system*,
not on behalf of a user); it enqueues per-user work that then runs fully scoped.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from jobpilot_core import repositories
from jobpilot_core.db import session_scope
from jobpilot_core.settings import get_settings
from sqlalchemy.exc import IntegrityError

from .celery_app import celery

logger = logging.getLogger("jobpilot.scheduler")

# Cadence keyword → seconds. A plain integer string is also accepted (seconds,
# floored at 60) so a demo can set a fast cadence without a code change.
_CADENCE = {
    "15m": 900, "every_15m": 900, "quarter_hourly": 900,
    "30m": 1800, "hourly": 3600, "1h": 3600,
    "2h": 7200, "6h": 21600, "12h": 43200,
    "daily": 86400, "1d": 86400, "weekly": 604800, "7d": 604800,
}


def schedule_interval(schedule: str | None) -> int | None:
    """Seconds between runs for this cadence, or None to skip (unknown/opt-out)."""
    if not schedule:
        return None
    key = schedule.strip().lower()
    if key in ("manual", "off", "none", "never"):
        return None
    if key in _CADENCE:
        return _CADENCE[key]
    if key.isdigit():
        return max(int(key), 60)
    return None


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


async def _guarded_enqueue(uid: uuid.UUID, settings) -> bool:
    """Open the umbrella (guarded) then enqueue the orchestrator. Returns False if
    a pipeline is already active (skip — don't stack)."""
    pid = uuid.uuid4()
    async with session_scope() as session:
        await repositories.reap_stale_pipelines(session, uid, settings.pipeline_stale_seconds)
    try:
        async with session_scope() as session:
            await repositories.open_pipeline(session, uid, pid)
    except IntegrityError:
        return False
    celery.send_task("pipeline.run_full", args=[str(uid), str(pid)],
                     queue=settings.orchestrator_queue)
    return True


async def run_scheduler_tick() -> dict:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    async with session_scope() as session:
        users = await repositories.list_scheduled_users(session)

    fired: list[str] = []
    skipped_active: list[str] = []
    not_due = 0
    for u in users:
        interval = schedule_interval(u["schedule"])
        if interval is None:
            continue
        last = _parse_iso(u["last_run_at"])
        due = last is None or (now - last).total_seconds() >= interval
        if not due:
            not_due += 1
            continue
        if await _guarded_enqueue(uuid.UUID(u["user_id"]), settings):
            fired.append(u["user_id"])
        else:
            skipped_active.append(u["user_id"])

    summary = {"fired": fired, "skipped_active": skipped_active, "not_due": not_due,
               "considered": len(users)}
    if fired or skipped_active:
        logger.info("scheduler.tick %s", summary)
    return summary
