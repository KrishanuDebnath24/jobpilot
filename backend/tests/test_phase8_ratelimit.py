"""Phase 8c — rate limiting & abuse protection (deterministic, offline).

Drives the limiter with an injected fake Redis (no real Redis needed): the fixed-window
counter throttles over the limit, fails OPEN on a Redis error (never locks users out),
and — over HTTP through a minimal app — returns a clear 429 with a Retry-After header.
"""

from __future__ import annotations

import pytest
from app import ratelimit
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient


class FakeRedis:
    """Minimal async Redis: INCR + EXPIRE over a dict (single window in a test run)."""

    def __init__(self):
        self.store: dict[str, int] = {}

    async def incr(self, key):
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    async def expire(self, key, ttl):
        return True


class BrokenRedis:
    async def incr(self, key):
        raise RuntimeError("redis down")

    async def expire(self, key, ttl):
        raise RuntimeError("redis down")


@pytest.fixture(autouse=True)
def _reset_client():
    yield
    ratelimit.set_client(None)  # restore the lazy real client for other tests


# --------------------------------------------------------------------------- #
# Core counter
# --------------------------------------------------------------------------- #
async def test_counter_throttles_over_the_limit():
    ratelimit.set_client(FakeRedis())
    # limit 3: first three allowed, the fourth is over
    results = [await ratelimit._too_many("trigger", "u1", 3, 60) for _ in range(4)]
    assert results == [False, False, False, True]
    # a different identity has its own bucket (per-user isolation)
    assert await ratelimit._too_many("trigger", "u2", 3, 60) is False


async def test_fail_open_on_redis_error():
    ratelimit.set_client(BrokenRedis())
    # a Redis outage must never block a legitimate request
    assert await ratelimit._too_many("trigger", "u1", 1, 60) is False


async def test_user_dependency_raises_429_over_limit():
    from fastapi import HTTPException

    ratelimit.set_client(FakeRedis())
    dep = ratelimit.user_rate_limit("trigger", 2)

    class _User:
        id = "user-abc"

    await dep(current_user=_User())  # 1
    await dep(current_user=_User())  # 2
    with pytest.raises(HTTPException) as ei:
        await dep(current_user=_User())  # 3 -> over
    assert ei.value.status_code == 429


# --------------------------------------------------------------------------- #
# HTTP surface — a clear 429 with Retry-After (auth/IP scope, no DB needed)
# --------------------------------------------------------------------------- #
def test_http_429_with_retry_after():
    ratelimit.set_client(FakeRedis())
    app = FastAPI()

    @app.get("/ping", dependencies=[Depends(ratelimit.ip_rate_limit("auth", 3))])
    def ping():
        return {"ok": True}

    client = TestClient(app)
    codes = [client.get("/ping").status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    resp = client.get("/ping")
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers
    assert "rate limit" in resp.json()["detail"].lower()
