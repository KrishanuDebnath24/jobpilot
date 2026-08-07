"""Resolve where each job's application form actually lives (apply agent, stage 2).

Reads jobs whose apply target is unknown or stale (db-mcp), follows each listing's
redirect chain to its terminal page, identifies the ATS hosting it, and writes the answer
back. No browser, no LLM, no submission — this stage only follows links, exactly as
clicking one would.

Politeness matters here in a way it did not for earlier stages: this is the first code
that touches employers' servers in bulk. Requests are capped by a concurrency semaphore
and spaced per host, so resolving 700 jobs never looks like a scrape to anyone receiving
it. A failure is recorded as data (``error`` on the target) and never aborts the batch —
one dead listing among hundreds must not lose the other results.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict

import httpx
from jobpilot_core import ats_detect

from . import mcp_client

logger = logging.getLogger("jobpilot.resolve")

#: Simultaneous in-flight requests across the whole batch.
MAX_CONCURRENCY = 6
#: Minimum gap between two requests to the *same* host, seconds.
PER_HOST_DELAY = 0.5
#: Per-request ceiling. Redirectors are usually fast; a slow one is not worth waiting on.
REQUEST_TIMEOUT = 12.0
#: Identify ourselves honestly rather than impersonating a browser.
USER_AGENT = "JobPilot/1.0 (+job application assistant; follows listing links only)"


async def _resolve_one(
    client: httpx.AsyncClient,
    job: dict,
    semaphore: asyncio.Semaphore,
    host_locks: dict[str, asyncio.Lock],
) -> dict:
    """Resolve a single job, serialising per host so one employer is never hammered."""
    url = job.get("url") or ""
    connector = job.get("connector")

    # Provenance settles it for direct-ATS connectors, so skip the semaphore, the host
    # lock and the politeness delay — none of them apply when no request is made.
    if ats_detect.from_connector(connector):
        target = await ats_detect.resolve_apply_url(client, url, connector=connector)
    else:
        host = ats_detect._host(url) or "-"
        async with semaphore:
            async with host_locks[host]:
                target = await ats_detect.resolve_apply_url(client, url, connector=connector)
                await asyncio.sleep(PER_HOST_DELAY)
    if target.error:
        logger.info("resolve: job=%s unresolved (%s)", job.get("id"), target.error)
    return {
        "id": job["id"],
        "apply_url": target.url or None,
        "apply_ats": target.ats,
        "apply_board_slug": target.board_slug,
        # The url we actually followed — lets a later scout rewriting `url` mark this stale.
        "apply_resolved_from": url,
    }


async def resolve_apply_targets(
    user_id: uuid.UUID, *, limit: int = 200, force: bool = False
) -> dict:
    """Resolve a batch of the user's jobs and persist the results.

    ``force`` re-resolves rows that already have an answer — the thing to run after
    teaching :mod:`jobpilot_core.ats_detect` a new platform, so previously "unknown"
    listings get a second look.
    """
    pending = (
        await mcp_client.db_mcp(
            user_id, "get_jobs_needing_apply_resolution", {"limit": limit, "force": force}
        )
    )["jobs"]
    if not pending:
        coverage = (await mcp_client.db_mcp(user_id, "get_apply_coverage", {}))["coverage"]
        return {"resolved": 0, "reachable": 0, "coverage": coverage}

    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    host_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=False,   # hops are counted and recorded, not delegated
    ) as client:
        targets = await asyncio.gather(
            *(_resolve_one(client, job, semaphore, host_locks) for job in pending),
            return_exceptions=False,   # _resolve_one already converts failure into data
        )

    written = (
        await mcp_client.db_mcp(user_id, "set_apply_targets", {"targets": targets})
    )["written"]
    coverage = (await mcp_client.db_mcp(user_id, "get_apply_coverage", {}))["coverage"]

    reachable = sum(
        1
        for t in targets
        if t["apply_ats"]
        not in (ats_detect.AGGREGATOR, ats_detect.UNKNOWN, ats_detect.BLOCKED)
    )
    logger.info(
        "resolve: user=%s attempted=%d written=%d reachable=%d",
        user_id, len(targets), written, reachable,
    )
    return {"resolved": written, "reachable": reachable, "coverage": coverage}
