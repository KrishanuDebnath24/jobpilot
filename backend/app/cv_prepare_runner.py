"""Prepare-CVs orchestration — the one-click Analyze → Align → Tailor/Export chain.

For a user's SELECTED jobs, produces a downloadable CV per job by chaining the existing
runners in order: analyze those jobs (the alignment loop needs each job's analysis) →
run the Rewriter⇄Critic loop until each CV matches its job → approve the fresh drafts
(export only renders approved) → Tailor them into A4 Irish-format PDF/DOCX. Auto-exported
so the CV attaches to each job row immediately; the qualifier then scores those jobs
against their attached CV. Per-job error isolation already lives in each runner; this
just sequences them. Reads/writes only via the runners' MCP paths.
"""

from __future__ import annotations

import logging
import uuid

from .analyze_runner import run_analyze
from .cv_align_runner import run_align
from .cv_export_runner import run_export
from .qualify_runner import run_qualify
from . import mcp_client

logger = logging.getLogger("jobpilot.cv_prepare")


async def run_prepare_cvs(user_id: uuid.UUID, job_ids: list[str]) -> dict:
    """Analyze → Align → approve → Tailor/Export for the selected jobs. Returns per-stage
    counts plus the created drafts so the UI can attach them to the job rows."""
    job_ids = list(dict.fromkeys(job_ids))
    if not job_ids:
        return {"analyzed": 0, "tailored": 0, "aligned": 0, "exported": 0,
                "drafts": [], "errors": {}}

    analyze = await run_analyze(user_id, job_ids=job_ids)
    # use_attested=False: this chain auto-approves and exports with no review stop, so it
    # stays on the strictly evidence-gated path. Accepted Gap-closer proposals (Phase 6.6)
    # are deliberately ignored here and only ride the reviewed "Tailor only" route.
    align = await run_align(user_id, job_ids, use_attested=False)
    drafts = align.get("drafts", [])
    draft_ids = [d["draft_id"] for d in drafts]

    exported = 0
    if draft_ids:
        # Export only renders APPROVED drafts (they're honest by construction), so the
        # one-click flow endorses the freshly aligned drafts before rendering.
        await mcp_client.db_mcp(
            user_id, "set_cv_draft_status", {"draft_ids": draft_ids, "status": "approved"})
        exp = await run_export(user_id, draft_ids)
        exported = exp.get("exported", 0)

    # Re-qualify immediately so the just-aligned jobs' scores reflect their new CV. Qualify
    # is incremental: only the freshly-aligned jobs changed cv_version, so it re-scores those
    # and skips the rest of the pool. Best-effort — the CVs are already attached either way.
    requalified = 0
    if exported:
        try:
            q = await run_qualify(user_id)
            requalified = q.get("scored", 0)
        except Exception as exc:  # noqa: BLE001 - requalify must never fail the attach
            logger.warning("cv_prepare.requalify failed (non-fatal): %s", exc)

    summary = {
        "analyzed": analyze.get("analyzed", analyze.get("count", 0)),
        "tailored": align.get("tailored", 0),
        "aligned": align.get("aligned", 0),  # of those, how many cleared the alignment bar
        "exported": exported,
        "requalified": requalified,
        "drafts": drafts,
        "errors": {**(analyze.get("errors") or {}), **(align.get("errors") or {})},
    }
    logger.info("cv_prepare.summary drafts=%d aligned=%d exported=%d requalified=%d errors=%d",
                summary["tailored"], summary["aligned"], summary["exported"],
                requalified, len(summary["errors"]))
    return summary
