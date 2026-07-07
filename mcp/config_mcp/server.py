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


# --------------------------------------------------------------------------- #
# Qualifier config (Phase 3)
# --------------------------------------------------------------------------- #
@mcp.tool()
async def get_qualifier_config() -> dict:
    """Return the caller's qualifier config (a sensible default shape if none exists)."""
    uid = require_user_id()
    async with session_scope() as session:
        cfg = await repositories.get_qualifier_config(session, uid)
        return {"config": repositories.qualifier_config_to_dict(cfg)}


@mcp.tool()
async def update_qualifier_config(
    weights_json: dict | None = None,
    threshold: float | None = None,
    must_have: list[str] | None = None,
    nice_to_have: list[str] | None = None,
) -> dict:
    """Create-or-update the caller's qualifier config (partial update). ``weights_json``
    = {skills,experience,title_keyword,location}; ``must_have`` missing → hard-disqualify."""
    uid = require_user_id()
    fields = {
        "weights_json": weights_json,
        "threshold": threshold,
        "must_have": must_have,
        "nice_to_have": nice_to_have,
    }
    async with session_scope() as session:
        cfg = await repositories.upsert_qualifier_config(session, uid, fields)
        return {"config": repositories.qualifier_config_to_dict(cfg)}


@mcp.tool()
async def ensure_qualifier_config(resume_skills: list[str] | None = None) -> dict:
    """Seed a conservative default (skills → nice-to-haves, no must-haves, moderate
    threshold) ONLY if the caller has no qualifier config yet. Idempotent."""
    from jobpilot_core.qualifier import default_qualifier_config

    uid = require_user_id()
    async with session_scope() as session:
        existing = await repositories.get_qualifier_config(session, uid)
        if existing is not None:
            return {"config": repositories.qualifier_config_to_dict(existing), "seeded": False}
        cfg = await repositories.upsert_qualifier_config(
            session, uid, default_qualifier_config(resume_skills or [])
        )
        return {"config": repositories.qualifier_config_to_dict(cfg), "seeded": True}


# --------------------------------------------------------------------------- #
# Gate config (Phase 6) — human-in-the-loop review toggles
# --------------------------------------------------------------------------- #
@mcp.tool()
async def get_gate_config() -> dict:
    """The caller's effective gate config (stored overrides merged onto the defaults):
    which pipeline transitions pause for review. Includes the default set for the UI."""
    from jobpilot_core import gates

    uid = require_user_id()
    async with session_scope() as session:
        stored = await repositories.get_gate_configs(session, uid)
    return {
        "gates": gates.effective_gates(stored),
        "defaults": dict(gates.GATE_DEFAULTS),
        "keys": list(gates.GATE_KEYS),
    }


@mcp.tool()
async def update_gate_config(gate_key: str, enabled: bool) -> dict:
    """Enable/disable a single review gate for the caller. Rejects unknown keys."""
    from jobpilot_core import gates

    uid = require_user_id()
    if gate_key not in gates.GATE_KEYS:
        return {"error": f"unknown gate {gate_key!r} (expected {list(gates.GATE_KEYS)})"}
    async with session_scope() as session:
        await repositories.upsert_gate_config(session, uid, gate_key, bool(enabled))
        stored = await repositories.get_gate_configs(session, uid)
    return {"gates": gates.effective_gates(stored)}


app = UserContextMiddleware(mcp.streamable_http_app(), settings)
