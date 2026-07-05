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
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator

import redis.asyncio as aioredis

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
