"""Readiness checks: DB + Redis connectivity (distinct from liveness /health)."""

from __future__ import annotations

import redis.asyncio as aioredis
from jobpilot_core.db import get_engine
from jobpilot_core.settings import Settings
from sqlalchemy import text


async def check_db() -> bool:
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def check_redis(settings: Settings) -> bool:
    client = aioredis.from_url(settings.redis_url)
    try:
        return bool(await client.ping())
    except Exception:
        return False
    finally:
        await client.aclose()
