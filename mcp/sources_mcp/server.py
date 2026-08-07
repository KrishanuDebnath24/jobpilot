"""sources-mcp — the connector hub over HTTP (streamable MCP).

One tool per connector behind a common interface; each returns the shared raw job
shape for the Scout to normalize + dedupe. A valid signed request context is
REQUIRED (same core as db-mcp) so anonymous callers can't burn a metered source's quota
(JSearch, Apify) or the Anthropic budget — even though the external calls themselves
aren't user-scoped.

This service makes LLM calls (crawl extraction), so it fail-fasts at boot if the
Anthropic key is missing/placeholder.
"""

from __future__ import annotations

from jobpilot_core.mcp_auth import UserContextMiddleware, require_user_id
from jobpilot_core.settings import get_settings
from mcp.server.fastmcp import FastMCP

from .connectors import apify, crawl, greenhouse, jsearch, themuse, workable
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
async def fetch_greenhouse(
    company: str,
    roles: list[str] | None = None,
    locations: list[str] | None = None,
    keywords: list[str] | None = None,
    roi_only: bool = True,
) -> dict:
    """Greenhouse board for one employer -> jobs in the common raw shape (ATS).
    ``roi_only`` on by default: a board is the company's whole global careers page."""
    require_user_id()
    jobs = await greenhouse.fetch_greenhouse(
        settings, company=company, roles=roles, locations=locations,
        keywords=keywords, roi_only=roi_only,
    )
    connector = f"greenhouse:{company.strip().lower()}"
    return {"connector": connector, "count": len(jobs), "jobs": [j.model_dump() for j in jobs]}


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
async def search_muse(
    roles: list[str] | None = None,
    keywords: list[str] | None = None,
    location: str = "Dublin, Ireland",
) -> dict:
    """The Muse public jobs API (free, full-text) -> common raw shape."""
    require_user_id()
    jobs = await themuse.search_muse(settings, roles=roles, keywords=keywords, location=location)
    return {"connector": "muse", "count": len(jobs), "jobs": [j.model_dump() for j in jobs]}


@mcp.tool()
async def search_workable(
    roles: list[str] | None = None,
    keywords: list[str] | None = None,
    location: str | None = None,
    max_results: int | None = None,
) -> dict:
    """Workable's Ireland feed (free, no key, full text, no employer list needed)
    -> common raw shape. Republic-of-Ireland only; Northern Ireland is excluded."""
    require_user_id()
    jobs = await workable.search_workable(
        settings, roles=roles, keywords=keywords, location=location, max_results=max_results
    )
    return {"connector": "workable", "count": len(jobs), "jobs": [j.model_dump() for j in jobs]}


@mcp.tool()
async def fetch_workable_board(
    company: str,
    roles: list[str] | None = None,
    keywords: list[str] | None = None,
    roi_only: bool = True,
) -> dict:
    """One employer's Workable board -> common raw shape (ATS, with a real apply URL).
    ``roi_only`` on by default: a board is the company's whole global careers page."""
    require_user_id()
    jobs = await workable.fetch_workable_board(
        settings, company=company, roles=roles, keywords=keywords, roi_only=roi_only
    )
    connector = f"workable:{company.strip().lower()}"
    return {"connector": connector, "count": len(jobs), "jobs": [j.model_dump() for j in jobs]}


@mcp.tool()
async def search_apify_linkedin(
    roles: list[str] | None = None,
    keywords: list[str] | None = None,
    location: str | None = None,
    max_results: int | None = None,
) -> dict:
    """LinkedIn via Apify (metered, OFF unless APIFY_API_TOKEN is set) -> common raw
    shape. Full text but no apply URL, so these are read-only, RoI-filtered."""
    require_user_id()
    jobs = await apify.search_apify_linkedin(
        settings, roles=roles, keywords=keywords, location=location, max_results=max_results
    )
    return {"connector": "apify:linkedin", "count": len(jobs), "jobs": [j.model_dump() for j in jobs]}


@mcp.tool()
async def run_crawl(
    roles: list[str] | None = None,
    keywords: list[str] | None = None,
    location: str = "dublin",
    max_pages: int | None = None,
) -> dict:
    """Polite single-board crawl (robots + rate limit): collect posting links from the
    search pages, then open each posting to read its full description from schema.org
    JSON-LD -> jobs in the common raw shape (crawl). Deterministic; usage is zero."""
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
