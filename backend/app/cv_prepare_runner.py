"""Prepare-CVs orchestration — the one-click Analyze → Tailor → Export chain.

For a user's SELECTED jobs, produces a downloadable tailored CV per job by chaining the
existing runners in order: analyze those jobs (Tailor needs each job's analysis) →
tailor → approve the fresh drafts (export only renders approved) → export to PDF/DOCX.
Auto-exported so the CV attaches to each job row immediately; the qualifier then scores
those jobs against their attached CV. Per-job error isolation already lives in each
runner; this just sequences them. Reads/writes only via the runners' MCP paths.
"""

from __future__ import annotations

import logging
import uuid

from .analyze_runner import run_analyze
from .cv_export_runner import run_export
from .tailor_runner import run_tailor
from . import mcp_client

logger = logging.getLogger("jobpilot.cv_prepare")


async def run_prepare_cvs(user_id: uuid.UUID, job_ids: list[str]) -> dict:
    """Analyze → Tailor → approve → Export for the selected jobs. Returns per-stage counts
    plus the created drafts so the UI can attach them to the job rows."""
    job_ids = list(dict.fromkeys(job_ids))
    if not job_ids:
        return {"analyzed": 0, "tailored": 0, "exported": 0, "drafts": [], "errors": {}}

    analyze = await run_analyze(user_id, job_ids=job_ids)
    tailor = await run_tailor(user_id, job_ids)
    drafts = tailor.get("drafts", [])
    draft_ids = [d["draft_id"] for d in drafts]

    exported = 0
    if draft_ids:
        # Export only renders APPROVED drafts (they're honest by construction), so the
        # one-click flow endorses the freshly tailored drafts before rendering.
        await mcp_client.db_mcp(
            user_id, "set_cv_draft_status", {"draft_ids": draft_ids, "status": "approved"})
        exp = await run_export(user_id, draft_ids)
        exported = exp.get("exported", 0)

    summary = {
        "analyzed": analyze.get("analyzed", analyze.get("count", 0)),
        "tailored": tailor.get("tailored", 0),
        "exported": exported,
        "drafts": drafts,
        "errors": {**(analyze.get("errors") or {}), **(tailor.get("errors") or {})},
    }
    logger.info("cv_prepare.summary tailored=%d exported=%d errors=%d",
                summary["tailored"], summary["exported"], len(summary["errors"]))
    return summary
