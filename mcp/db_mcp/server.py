"""db-mcp — scoped CRUD over HTTP (streamable MCP).

Security core: the caller's ``user_id`` is derived ONLY from the signed request
context (validated by :class:`UserContextMiddleware`). Any ``user_id`` a caller
passes as a tool argument is accepted and then **ignored** — see ``get_jobs`` /
``delete_user_data``. Unauthenticated requests raise and return an error.
"""

from __future__ import annotations

import asyncio
import uuid

from jobpilot_core import repositories
from jobpilot_core.db import session_scope
from jobpilot_core.mcp_auth import UserContextMiddleware, require_user_id
from jobpilot_core.schemas import JobIn
from jobpilot_core.settings import get_settings
from jobpilot_core.storage import get_storage, user_prefix
from mcp.server.fastmcp import FastMCP

settings = get_settings()
mcp = FastMCP("db-mcp", stateless_http=True, json_response=True, host="0.0.0.0", port=8001)


@mcp.tool()
async def get_jobs(
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
    user_id: str | None = None,  # IGNORED — scoping comes from the request context
) -> dict:
    uid = require_user_id()
    async with session_scope() as session:
        jobs = await repositories.get_jobs(session, uid, limit=limit, offset=offset, status=status)
        return {"jobs": [repositories.job_to_dict(j) for j in jobs]}


@mcp.tool()
async def get_job(job_id: str) -> dict:
    uid = require_user_id()
    async with session_scope() as session:
        job = await repositories.get_job(session, uid, uuid.UUID(job_id))
        return {"job": repositories.job_to_dict(job) if job else None}


@mcp.tool()
async def upsert_jobs(jobs: list[dict]) -> dict:
    uid = require_user_id()
    parsed = [JobIn.model_validate(j) for j in jobs]
    async with session_scope() as session:
        saved = await repositories.upsert_jobs(session, uid, parsed)
        return {"jobs": [repositories.job_to_dict(j) for j in saved]}


@mcp.tool()
async def search_jobs(query: str, limit: int = 20) -> dict:
    """Phase-1 keyword fallback (semantic embedding search lands in Phase 3)."""
    uid = require_user_id()
    async with session_scope() as session:
        jobs = await repositories.search_jobs(session, uid, query, limit=limit)
        return {"jobs": [repositories.job_to_dict(j) for j in jobs], "mode": "keyword"}


@mcp.tool()
async def get_resume() -> dict:
    uid = require_user_id()
    async with session_scope() as session:
        resume = await repositories.get_latest_resume(session, uid)
        return {"resume": repositories.resume_to_dict(resume) if resume else None}


@mcp.tool()
async def get_qualifications() -> dict:
    uid = require_user_id()
    async with session_scope() as session:
        return {"qualifications": await repositories.get_qualifications(session, uid)}


@mcp.tool()
async def get_analyses() -> dict:
    uid = require_user_id()
    async with session_scope() as session:
        return {"analyses": await repositories.get_analyses(session, uid)}


@mcp.tool()
async def get_suggestions() -> dict:
    uid = require_user_id()
    async with session_scope() as session:
        return {"suggestions": await repositories.get_suggestions(session, uid)}


@mcp.tool()
async def update_suggestion_status(suggestion_id: str, status: str) -> dict:
    uid = require_user_id()
    async with session_scope() as session:
        s = await repositories.update_suggestion_status(session, uid, uuid.UUID(suggestion_id), status)
        return {"suggestion": {"id": str(s.id), "status": s.status} if s else None}


@mcp.tool()
async def delete_user_data(user_id: str | None = None) -> dict:
    """GDPR erasure: hard-delete every row keyed to the caller (cascade) AND their
    stored resume files. ``user_id`` argument is IGNORED; scope comes from context.
    """
    uid = require_user_id()
    storage = get_storage(settings)
    # 1) delete storage objects for this user (best-effort, outside the DB tx)
    files_deleted = await asyncio.to_thread(storage.delete_prefix, user_prefix(uid))
    # 2) delete DB rows (users -> cascade), then verify zero remain in-transaction
    async with session_scope() as session:
        await repositories.delete_user_cascade(session, uid)
        await session.flush()
        remaining = await repositories.count_user_rows(session, uid)
    return {"deleted": True, "files_deleted": files_deleted, "remaining_counts": remaining}


# ASGI app: validate the signed context on every request, bind user_id for the call.
app = UserContextMiddleware(mcp.streamable_http_app(), settings)
