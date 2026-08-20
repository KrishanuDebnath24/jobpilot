"""Durable progress records for long background runs (gap-close, and anything like it).

The pipeline stages already publish to the Redis pub/sub bus (:mod:`jobpilot_core.events`),
but pub/sub is fire-and-forget: a message is dropped if nobody is subscribed at that instant.
A gap-close run can sit in an Anthropic batch for a long time, and the user is expected to
reload the page or walk away during it — so its progress needs to be *readable at any moment*,
not only while a listener happens to be attached.

So this is a plain last-write-wins key per (user, kind) with a long TTL: the runner overwrites
it as it advances, and the UI polls it. Fail-soft everywhere — a Redis outage must degrade the
progress display, never the run itself.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as aioredis
from jobpilot_core.settings import get_settings

logger = logging.getLogger("jobpilot.progress")

_PREFIX = "jobpilot:progress"
# Outlives any run: an Anthropic batch expires at 24h, so a wait can legitimately be that
# long, and the finished record should still be there to read afterwards.
_TTL_SECONDS = 48 * 3600

_client: Any = None


def _redis() -> Any:
    global _client
    if _client is None:
        _client = aioredis.from_url(get_settings().redis_url, decode_responses=True)
    return _client


def set_client(client: Any) -> None:
    """Inject a Redis-like async client (``set``/``get``/``delete``) — for tests."""
    global _client
    _client = client


def key(kind: str, user_id: uuid.UUID | str) -> str:
    return f"{_PREFIX}:{kind}:{user_id}"


async def set_progress(kind: str, user_id: uuid.UUID | str, payload: dict) -> None:
    """Overwrite this run's progress record. Never raises."""
    body = {**payload, "kind": kind, "ts": datetime.now(timezone.utc).isoformat()}
    try:
        await _redis().set(key(kind, user_id), json.dumps(body), ex=_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001 - progress must never sink the run
        logger.warning("progress: write failed kind=%s: %s", kind, exc)


async def get_progress(kind: str, user_id: uuid.UUID | str) -> dict | None:
    """The last record written, or None if there has never been one (or Redis is down)."""
    try:
        raw = await _redis().get(key(kind, user_id))
        return json.loads(raw) if raw else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("progress: read failed kind=%s: %s", kind, exc)
        return None


async def clear_progress(kind: str, user_id: uuid.UUID | str) -> None:
    try:
        await _redis().delete(key(kind, user_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("progress: delete failed kind=%s: %s", kind, exc)
