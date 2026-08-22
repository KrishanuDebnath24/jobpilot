"""Redis pub/sub event bus for pipeline progress (Phase 4).

The **fast path** for pipeline progression and the live UI. Stages publish
``<stage>.started`` / ``.progress`` / ``.completed`` / ``.failed`` on a per-user
channel; the orchestrator and the SSE relay subscribe to it.

Critical property: pub/sub is **fire-and-forget** — a message is dropped if no
subscriber is listening at that instant. So events only ever *accelerate* a
decision; the source of truth for "did the stage finish?" is always the persisted
``pipeline_runs`` row (see ``repositories.get_stage_status``). Never gate a state
transition on an event alone.

Channels are per-user (``jobpilot:events:{user_id}``) so a subscriber only ever
sees its own user's events — one user's status can't leak onto another's stream.
"""

from __future__ import annotations

import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

import redis.asyncio as aioredis

logger = logging.getLogger("jobpilot.events")

_PREFIX = "jobpilot:events"

# Event type suffixes (stage-qualified, e.g. "scout.completed", "pipeline.started").
STARTED = "started"
PROGRESS = "progress"
COMPLETED = "completed"
FAILED = "failed"


def user_channel(user_id: uuid.UUID | str) -> str:
    return f"{_PREFIX}:{user_id}"


def make_event(
    stage: str,
    kind: str,
    *,
    pipeline_id: uuid.UUID | str | None = None,
    counts: dict | None = None,
    **extra: Any,
) -> dict:
    """Build a JSON-safe event envelope. ``type`` is ``"{stage}.{kind}"``."""
    return {
        "type": f"{stage}.{kind}",
        "stage": stage,
        "kind": kind,
        "pipeline_id": str(pipeline_id) if pipeline_id else None,
        "counts": counts,
        "ts": datetime.now(timezone.utc).isoformat(),
        **extra,
    }


# --------------------------------------------------------------------------- #
# Liveness heartbeat
#
# A ``pipeline_runs`` row is opened by the worker and closed by the worker. If the worker
# dies in between — OOM, a container restart, a laptop closing — nothing closes it, and the
# row says "running" forever: the UI shows a spinner for a task that no longer exists.
#
# Age alone cannot distinguish that from a stage that is merely slow, and reaping a live run
# is far worse than showing a stale one. So a running stage writes a heartbeat, and only a
# stage that has stopped beating is considered dead. Fail-soft in both directions: a Redis
# outage must never fail a run (the write is swallowed) and must never mass-reap live runs
# (an unreadable heartbeat is reported as "unknown", and the caller keeps the row).
# --------------------------------------------------------------------------- #
_BEAT_PREFIX = "jobpilot:beat"
_BEAT_TTL_SECONDS = 6 * 3600  # comfortably longer than any stage; the timestamp is the signal


def beat_key(user_id: uuid.UUID | str, stage: str) -> str:
    return f"{_BEAT_PREFIX}:{user_id}:{stage}"


async def beat(redis_url: str, user_id: uuid.UUID | str, stage: str) -> None:
    """Record that ``stage`` is still alive for this user. Never raises."""
    try:
        client = aioredis.from_url(redis_url, decode_responses=True)
        try:
            await client.set(
                beat_key(user_id, stage),
                datetime.now(timezone.utc).isoformat(),
                ex=_BEAT_TTL_SECONDS,
            )
        finally:
            await client.aclose()
    except Exception as exc:  # noqa: BLE001 - liveness bookkeeping must not sink a run
        logger.warning("events.beat failed stage=%s: %s", stage, exc)


async def alive_stages(
    redis_url: str, user_id: uuid.UUID | str, stages: list[str], stale_seconds: int
) -> set[str]:
    """Which of ``stages`` have beaten within ``stale_seconds``.

    On any Redis error every stage is reported alive, so a broker hiccup can never be read
    as "every run died" — the reaper then does nothing, which is the safe direction."""
    try:
        client = aioredis.from_url(redis_url, decode_responses=True)
        try:
            values = await client.mget([beat_key(user_id, s) for s in stages])
        finally:
            await client.aclose()
    except Exception as exc:  # noqa: BLE001
        logger.warning("events.alive_stages failed: %s", exc)
        return set(stages)  # unknown == alive; never reap on a Redis failure

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)
    alive: set[str] = set()
    for stage, raw in zip(stages, values):
        if not raw:
            continue
        try:
            if datetime.fromisoformat(raw) >= cutoff:
                alive.add(stage)
        except ValueError:
            alive.add(stage)  # unparseable: treat as alive rather than reap on a bad read
    return alive


async def publish_event(redis_url: str, user_id: uuid.UUID | str, event: dict) -> int:
    """Publish one event to the user's channel. One-shot connection — stages emit
    only a handful of events per run, so a persistent client isn't worth the
    lifecycle complexity. Returns the subscriber count (0 = nobody listening; that's
    fine, the DB row is still the truth)."""
    payload = {**event, "user_id": str(user_id)}
    client = aioredis.from_url(redis_url)
    try:
        return await client.publish(user_channel(user_id), json.dumps(payload))
    finally:
        await client.aclose()


@asynccontextmanager
async def subscribe(redis_url: str, user_id: uuid.UUID | str):
    """Async context manager yielding a pubsub subscribed to the user's channel."""
    client = aioredis.from_url(redis_url, decode_responses=True)
    pubsub = client.pubsub()
    await pubsub.subscribe(user_channel(user_id))
    try:
        yield pubsub
    finally:
        try:
            await pubsub.unsubscribe(user_channel(user_id))
            await pubsub.aclose()
        finally:
            await client.aclose()


async def listen(pubsub, timeout: float = 1.0) -> AsyncIterator[dict | None]:
    """Yield parsed event dicts from a subscribed pubsub. Yields ``None`` on each
    idle ``timeout`` tick so callers can interleave a DB reconciliation poll (the
    fallback that makes a dropped event non-fatal)."""
    while True:
        msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=timeout)
        if msg is None:
            yield None
            continue
        data = msg.get("data")
        if not data:
            yield None
            continue
        try:
            yield json.loads(data)
        except (ValueError, TypeError):
            yield None
