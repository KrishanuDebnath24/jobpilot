"""Gap-close orchestration (Phase 6.6) — generate the attested-CV proposals per job.

For the user's **selected** jobs: make sure each has a Phase-5 analysis (analysing the
ones that don't, so this entry point is self-sufficient, exactly like ``run_align``), then
run the unconstrained :mod:`jobpilot_core.gap_closer` over each analysis and persist the
proposal set with per-item ids and ``accepted: None``.

Nothing here reaches a CV. This produces *proposals* for the UI; the alignment loop only
ever sees the items the user later accepts (:func:`jobpilot_core.gap_closer.accepted_items`).
Regenerating a job replaces its whole set — including any accept state — because a proposal
the user endorsed no longer exists once the wording it referred to is gone.

Reads via MCP; writes via db-mcp. Per-job error isolation; bounded selection.
"""

from __future__ import annotations

import logging
import uuid

from jobpilot_core import gap_closer
from jobpilot_core.llm import LLMUsage
from jobpilot_core.settings import get_settings

from . import mcp_client

logger = logging.getLogger("jobpilot.gap_close")


async def _analyses_by_job(user_id: uuid.UUID) -> dict[str, dict]:
    analyses = (await mcp_client.db_mcp(user_id, "get_analyses", {}))["analyses"]
    out: dict[str, dict] = {}
    for a in analyses:
        jid = a.get("job_id")
        ex = a.get("extracted_json") or {}
        if jid and jid not in out:  # newest-first from db-mcp; keep the latest
            out[jid] = {
                **ex,
                "gaps_vs_resume": ex.get("gaps_vs_resume") or (a.get("gaps_json") or {}).get("gaps"),
            }
    return out


async def _ensure_analyses(user_id: uuid.UUID, job_ids: list[str]) -> dict[str, dict]:
    """Analyses for the selection, analysing any job that hasn't been analysed yet."""
    analyses = await _analyses_by_job(user_id)
    missing = [jid for jid in job_ids if jid not in analyses]
    if not missing:
        return analyses
    from .analyze_runner import run_analyze

    logger.info("gap_close.analyzing %d selected job(s) with no analysis", len(missing))
    await run_analyze(user_id, job_ids=missing)
    return await _analyses_by_job(user_id)


async def run_gap_close(user_id: uuid.UUID, job_ids: list[str]) -> dict:
    """Propose gap-closing CV content for each selected job. Returns per-job counts plus
    the proposal sets, so the UI can render them for review without a second round-trip."""
    settings = get_settings()
    job_ids = list(dict.fromkeys(job_ids))[: settings.gap_close_max_jobs]
    if not job_ids:
        return {"proposed": 0, "jobs": {}, "errors": {}, "llm_usage": {}}

    resume_wrap = (await mcp_client.resume_mcp(user_id, "get_base_resume", {}))["resume"]
    if not resume_wrap or not resume_wrap.get("structured"):
        raise RuntimeError("no uploaded resume on file — upload a resume before proposing")
    resume = resume_wrap["structured"]

    analyses = await _ensure_analyses(user_id, job_ids)
    usage = LLMUsage(model=settings.enhancer_model)
    errors: dict[str, str] = {}
    jobs_out: dict[str, dict] = {}

    for jid in job_ids:
        analysis = analyses.get(jid)
        if not analysis:
            errors[jid] = "no analysis could be produced for this job"
            continue
        try:
            res = await gap_closer.propose_gap_close(analysis, resume, settings)
            if not res.data:
                errors[jid] = "gap-closer returned no proposals"
                continue
            usage.add(res.usage.input_tokens, res.usage.output_tokens,
                      cache_read=res.usage.cache_read_input_tokens,
                      cache_creation=res.usage.cache_creation_input_tokens)
            shaped = gap_closer.with_item_ids(res.data)
            await mcp_client.db_mcp(user_id, "upsert_gap_close",
                                    {"job_id": jid, "gap_close": shaped})
            jobs_out[jid] = shaped
        except Exception as exc:  # noqa: BLE001 - one bad job must not sink the batch
            errors[jid] = f"{type(exc).__name__}: {exc}"
            logger.warning("gap_close.job id=%s FAILED: %s", jid, exc)

    summary = {
        "proposed": len(jobs_out),
        "jobs": jobs_out,
        "errors": errors,
        "llm_usage": usage.as_dict(),
    }
    logger.info("gap_close.summary jobs=%d errors=%d cost=%.6f",
                len(jobs_out), len(errors), usage.cost_usd)
    return summary
