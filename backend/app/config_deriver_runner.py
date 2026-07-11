"""Config-deriver orchestration — fetch the user's parsed resume (via resume-mcp) and
run the LLM proposal (in ``jobpilot_core.config_deriver``).

Returns a *proposal only* — a market-canonical ``{roles, keywords, locations, ...}`` the
frontend shows in a review dialog. Persisting the confirmed values is a separate
``update_search_config`` call (config-mcp), so this path never writes the DB. LLM
orchestration lives here in the backend (like the other runners), keeping config-mcp
deterministic/DB-only.
"""

from __future__ import annotations

import uuid

from jobpilot_core import config_deriver
from jobpilot_core.settings import get_settings

from . import mcp_client


async def propose_config(user_id: uuid.UUID, hint: str | None = None) -> dict:
    """Fetch the caller's structured resume and propose a canonical search config. A
    missing resume yields an empty proposal (nothing to derive from)."""
    settings = get_settings()
    wrap = (await mcp_client.resume_mcp(user_id, "get_structured_resume", {})).get("resume")
    resume = wrap.get("structured") if wrap else None
    proposal = await config_deriver.propose_search_config(resume, hint, settings)
    return {"proposal": proposal, "has_resume": bool(resume)}
