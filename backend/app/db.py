"""FastAPI DB dependency backed by the shared async sessionmaker.

Auth/login is allowed to touch the DB directly (it is not an MCP data path). Job
and resume *data* paths go through the MCP services instead — see app.mcp_client.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from jobpilot_core.db import get_sessionmaker
from sqlalchemy.ext.asyncio import AsyncSession


async def get_db() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
