"""sources-mcp — the connector hub over HTTP (streamable MCP).

One tool per connector behind a common interface; each returns the shared raw job
shape for the Scout to normalize + dedupe. A valid signed request context is
REQUIRED (same core as db-mcp) so anonymous callers can't burn our Adzuna quota or
Anthropic budget — even though the external calls themselves aren't user-scoped.

This service makes LLM calls (crawl extraction), so it fail-fasts at boot if the
Anthropic key is missing/placeholder.
"""

from __future__ import annotations

from jobpilot_core.mcp_auth import UserContextMiddleware, require_user_id
from jobpilot_core.settings import get_settings
from mcp.server.fastmcp import FastMCP

from .connectors import adzuna, careerjet, crawl, greenhouse, jooble, jsearch
from .connectors.base import SOURCES

settings = get_settings()
# Fail-fast: this phase requires a real Anthropic key wherever LLM calls are made.
settings.require_anthropic_key()

mcp = FastMCP("sources-mcp", stateless_http=True, json_response=True, host="0.0.0.0", port=8004)


@mcp.tool()
async def list_sources() -> dict:
    """List the connectors this hub can run (name + source_type)."""
    require_user_id()
    return {"sources": SOURCES}


@mcp.tool()
async def search_adzuna(
    roles: list[str] | None = None,
    locations: list[str] | None = None,
    keywords: list[str] | None = None,
    max_results: int | None = None,
) -> dict:
    """Adzuna backbone feed -> jobs in the common raw shape (bounded by quota)."""
    require_user_id()  # scope-gate the resource, not the (public) external data
    jobs = await adzuna.search_adzuna(
        settings, roles=roles, locations=locations, keywords=keywords, max_results=max_results
    )
    return {"connector": "adzuna", "count": len(jobs), "jobs": [j.model_dump() for j in jobs]}


@mcp.tool()
async def fetch_greenhouse(
    company: str,
    roles: list[str] | None = None,
    locations: list[str] | None = None,
    keywords: list[str] | None = None,
) -> dict:
    """Greenhouse board for one employer -> jobs in the common raw shape (ATS)."""
    require_user_id()
    jobs = await greenhouse.fetch_greenhouse(
        settings, company=company, roles=roles, locations=locations, keywords=keywords
    )
    connector = f"greenhouse:{company.strip().lower()}"
    return {"connector": connector, "count": len(jobs), "jobs": [j.model_dump() for j in jobs]}


@mcp.tool()
async def search_jooble(
    roles: list[str] | None = None,
    keywords: list[str] | None = None,
    location: str = "Dublin, Ireland",
) -> dict:
    """Jooble aggregator (free-string RoI location) -> common raw shape."""
    require_user_id()
    jobs = await jooble.search_jooble(settings, roles=roles, keywords=keywords, location=location)
    return {"connector": "jooble", "count": len(jobs), "jobs": [j.model_dump() for j in jobs]}


@mcp.tool()
async def search_careerjet(
    roles: list[str] | None = None,
    keywords: list[str] | None = None,
    location: str = "Dublin",
) -> dict:
    """Careerjet aggregator (Ireland site, en_IE locale) -> common raw shape."""
    require_user_id()
    jobs = await careerjet.search_careerjet(settings, roles=roles, keywords=keywords, location=location)
    return {"connector": "careerjet", "count": len(jobs), "jobs": [j.model_dump() for j in jobs]}


@mcp.tool()
async def search_jsearch(
    roles: list[str] | None = None,
    keywords: list[str] | None = None,
    location: str = "Dublin, Ireland",
    num_pages: int | None = None,
) -> dict:
    """JSearch/Google-for-Jobs (metered; country=ie) -> common raw shape."""
    require_user_id()
    jobs = await jsearch.search_jsearch(
        settings, roles=roles, keywords=keywords, location=location, num_pages=num_pages
    )
    return {"connector": "jsearch", "count": len(jobs), "jobs": [j.model_dump() for j in jobs]}


@mcp.tool()
async def run_crawl(
    roles: list[str] | None = None,
    keywords: list[str] | None = None,
    location: str = "dublin",
    max_pages: int | None = None,
) -> dict:
    """Polite single-board crawl (robots + rate limit) with constrained Haiku
    extraction -> jobs in the common raw shape (crawl). Reports LLM token usage."""
    require_user_id()
    jobs, usage = await crawl.run_crawl(
        settings, roles=roles, keywords=keywords, location=location, max_pages=max_pages
    )
    return {
        "connector": f"crawl:{settings.crawler_board}",
        "count": len(jobs),
        "jobs": [j.model_dump() for j in jobs],
        "usage": usage.as_dict(),
    }


app = UserContextMiddleware(mcp.streamable_http_app(), settings)
