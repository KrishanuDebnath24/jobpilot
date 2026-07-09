"""Per-user / per-IP rate limiting (Phase 8c — abuse protection).

A fixed-window counter over Redis (the broker we already run): ``INCR`` a
``rl:{scope}:{identity}:{window}`` key and ``EXPIRE`` it on first hit; over the limit →
HTTP 429. Applied as FastAPI dependencies so neither a user nor a runaway loop can hammer
the pipeline / the LLM, and login can't be brute-forced.

**Fail-open by design:** a Redis outage must never lock legitimate users out — a failed
counter check logs and allows the request (the account/code spend caps and the
one-active-pipeline DB guard remain as backstops). Identity is the authenticated
``user_id`` for user scopes, the client IP for the unauthenticated auth scope. The Redis
client is injectable so the limiter is unit-testable offline (no real Redis needed).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

import redis.asyncio as aioredis
from fastapi import Depends, HTTPException, Request, status
from jobpilot_core.settings import get_settings

from .auth import get_current_user

logger = logging.getLogger("jobpilot.ratelimit")

_client: Any = None


def _redis() -> Any:
    global _client
    if _client is None:
        _client = aioredis.from_url(get_settings().redis_url, decode_responses=True)
    return _client


def set_client(client: Any) -> None:
    """Inject a Redis-like async client (``incr``/``expire``) — for tests."""
    global _client
    _client = client


async def _too_many(scope: str, identity: str, limit: int, window: int) -> bool:
    """Increment this window's counter; return True if the caller is over the limit.
    Fail-open: any Redis error returns False (allow) so an outage never blocks users."""
    try:
        r = _redis()
        bucket = int(time.time()) // window
        key = f"rl:{scope}:{identity}:{bucket}"
        n = int(await r.incr(key))
        if n == 1:
            await r.expire(key, window)
        return n > limit
    except Exception as exc:  # noqa: BLE001 - fail-open
        logger.warning("ratelimit: check failed for scope=%s (allowing): %s", scope, exc)
        return False


def _reject(scope: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=f"Rate limit exceeded for '{scope}'. Please slow down and retry shortly.",
        headers={"Retry-After": str(get_settings().rate_limit_window_seconds)},
    )


def user_rate_limit(scope: str, per_min: Callable[[], int] | int) -> Callable:
    """Dependency: limit ``scope`` per authenticated user. ``per_min`` may be an int or a
    zero-arg getter (so the limit is read from live settings, not frozen at import)."""

    async def dep(current_user=Depends(get_current_user)) -> None:
        settings = get_settings()
        if not settings.rate_limit_enabled:
            return
        limit = per_min() if callable(per_min) else per_min
        if await _too_many(scope, str(current_user.id), limit, settings.rate_limit_window_seconds):
            logger.warning("ratelimit: user over limit scope=%s", scope)  # no PII (scope only)
            raise _reject(scope)

    return dep


def ip_rate_limit(scope: str, per_min: Callable[[], int] | int) -> Callable:
    """Dependency: limit ``scope`` per client IP (for unauthenticated endpoints)."""

    async def dep(request: Request) -> None:
        settings = get_settings()
        if not settings.rate_limit_enabled:
            return
        limit = per_min() if callable(per_min) else per_min
        ip = request.client.host if request.client else "unknown"
        if await _too_many(scope, ip, limit, settings.rate_limit_window_seconds):
            logger.warning("ratelimit: ip over limit scope=%s", scope)
            raise _reject(scope)

    return dep
