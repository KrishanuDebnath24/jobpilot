"""Run an async coroutine from a sync Celery task — safely, once per event loop.

Celery prefork runs each task in a child process and we drive async work with
``asyncio.run`` (a fresh loop per task). The shared SQLAlchemy **async** engine
caches asyncpg connections bound to the loop that created them; reusing that cached
engine on a later task's new loop raises "got Future attached to a different loop"
/ "Event loop is closed". So we dispose + reset the engine at the end of every task
(inside its own loop, cleanly), forcing the next task to build a fresh one.

MCP-only tasks (Scout/Qualify go over HTTP) don't touch the engine — dispose is a
no-op there — so this is safe to wrap around every task uniformly.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Awaitable, TypeVar

# psycopg async (LangGraph checkpointer) needs a selector loop; Linux already uses
# one, so this only matters if a worker is ever run on Windows.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

T = TypeVar("T")


async def _guarded(coro: Awaitable[T]) -> T:
    try:
        return await coro
    finally:
        import jobpilot_core.db as db

        engine = db._engine
        db._engine = None
        db._sessionmaker = None
        if engine is not None:
            await engine.dispose()  # close asyncpg conns on THIS loop, before it ends


def run_async(coro: Awaitable[T]) -> T:
    return asyncio.run(_guarded(coro))
