"""config-mcp — user search configuration over HTTP (streamable MCP).

Same security core as db-mcp: the caller's ``user_id`` comes ONLY from the signed
request context (validated by :class:`UserContextMiddleware`). A second user can
never read or write another user's config — scoping is bound by the runtime, not
by any argument the caller supplies.
"""

from __future__ import annotations

from jobpilot_core import repositories
from jobpilot_core.db import session_scope
from jobpilot_core.mcp_auth import UserContextMiddleware, require_user_id
from jobpilot_core.settings import get_settings
from mcp.server.fastmcp import FastMCP

settings = get_settings()
mcp = FastMCP("config-mcp", stateless_http=True, json_response=True, host="0.0.0.0", port=8003)


@mcp.tool()
async def get_search_config() -> dict:
    """Return the caller's search config (a default empty shape if none exists)."""
    uid = require_user_id()
    async with session_scope() as session:
        cfg = await repositories.get_search_config(session, uid)
        return {"config": repositories.search_config_to_dict(cfg)}


@mcp.tool()
async def update_search_config(
    roles: list[str] | None = None,
    locations: list[str] | None = None,
    keywords: list[str] | None = None,
    sources: list[str] | None = None,
    target_employers: list[str] | None = None,
    sites: list[str] | None = None,
    schedule: str | None = None,
) -> dict:
    """Create-or-update the caller's search config (partial update: only provided
    fields are written). ``sources`` = enabled connectors; ``target_employers`` =
    ATS boards to poll."""
    uid = require_user_id()
    fields = {
        "roles": roles,
        "locations": locations,
        "keywords": keywords,
        "sources": sources,
        "target_employers": target_employers,
        "sites": sites,
        "schedule": schedule,
    }
    async with session_scope() as session:
        cfg = await repositories.upsert_search_config(session, uid, fields)
        return {"config": repositories.search_config_to_dict(cfg)}


app = UserContextMiddleware(mcp.streamable_http_app(), settings)
