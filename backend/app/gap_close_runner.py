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
from datetime import datetime, timezone

from jobpilot_core import gap_closer, gap_critic
from jobpilot_core.llm import LLMUsage
from jobpilot_core.settings import get_settings

from . import mcp_client, run_progress

logger = logging.getLogger("jobpilot.gap_close")

PROGRESS_KIND = "gap_close"


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


async def _ensure_analyses(
    user_id: uuid.UUID, job_ids: list[str], report=None
) -> dict[str, dict]:
    """Analyses for the selection, analysing any job that hasn't been analysed yet.

    Waits for the analyze step however long it takes (``unbounded_batch``): above
    ``analyze_batch_threshold`` jobs this goes through the Batch API, and abandoning that
    wait means throwing away work that has already been paid for. The batch still reaches a
    terminal state on its own — Anthropic expires one at 24h — so the wait can't hang forever.
    """
    analyses = await _analyses_by_job(user_id)
    missing = [jid for jid in job_ids if jid not in analyses]
    if not missing:
        return analyses
    from .analyze_runner import run_analyze

    logger.info("gap_close.analyzing %d selected job(s) with no analysis", len(missing))
    if report is not None:
        await report(phase="analyzing", analyzing=len(missing))

    async def _on_wait(info: dict) -> None:
        if report is not None:
            await report(phase="analyzing", analyzing=len(missing), **info)

    await run_analyze(user_id, job_ids=missing, unbounded_batch=True, on_wait=_on_wait)
    return await _analyses_by_job(user_id)


async def _refine(
    shaped: dict,
    analysis: dict,
    resume: dict,
    settings,
    usage: LLMUsage,
    jid: str,
    errors: dict,
) -> dict:
    """Critique the proposals, then re-propose against that critique.

    Returns the improved set, or the original unchanged whenever the extra work would not
    pay for itself: a critic that finds nothing, or a second pass that comes back empty.
    Failures here are deliberately non-fatal — a refine that breaks should cost the user
    the improvement, not the proposals they already have.
    """
    try:
        verdict = await gap_critic.critique_proposals(shaped, analysis, settings)
        if not verdict.data:
            return shaped
        usage.add(verdict.usage.input_tokens, verdict.usage.output_tokens,
                  cache_read=verdict.usage.cache_read_input_tokens,
                  cache_creation=verdict.usage.cache_creation_input_tokens)
        if not gap_critic.needs_revision(verdict.data):
            # The critic passed it. A second propose call would spend tokens to churn
            # content the reviewer already judged good.
            return shaped

        second = await gap_closer.propose_gap_close(
            analysis, resume, settings,
            critique=gap_critic.critique_block(verdict.data),
        )
        if not second.data:
            return shaped
        usage.add(second.usage.input_tokens, second.usage.output_tokens,
                  cache_read=second.usage.cache_read_input_tokens,
                  cache_creation=second.usage.cache_creation_input_tokens)
        return gap_closer.with_item_ids(second.data)
    except Exception as exc:  # noqa: BLE001 - keep the first-pass proposals
        logger.warning("gap_close.refine id=%s failed, keeping first pass: %s", jid, exc)
        errors[f"{jid}:refine"] = f"{type(exc).__name__}: {exc}"
        return shaped


async def run_gap_close(user_id: uuid.UUID, job_ids: list[str], refine: bool = False) -> dict:
    """Propose gap-closing CV content for each selected job. Returns per-job counts plus
    the proposal sets, so the UI can render them for review without a second round-trip.

    Runs to completion rather than to a clock: the analyze step it depends on waits out its
    batch however long that takes, and a failure there no longer discards the run — jobs that
    *do* have an analysis are still proposed for. Progress is written to a durable record
    (:mod:`app.run_progress`) on every phase change and after every job, because this can run
    far longer than anyone will sit and watch it.
    """
    settings = get_settings()
    requested = len(list(dict.fromkeys(job_ids)))
    job_ids = list(dict.fromkeys(job_ids))[: settings.gap_close_max_jobs]
    started = datetime.now(timezone.utc).isoformat()

    state: dict = {
        "phase": "starting",
        "refine": refine,
        "requested": requested,
        "total": len(job_ids),
        # > 0 means the spend guard (gap_close_max_jobs) dropped the tail of the selection.
        "dropped": requested - len(job_ids),
        "done": 0,
        "job_ids": job_ids,
        "started_at": started,
        "errors": {},
    }

    async def report(**over) -> None:
        """Merge into the durable record and write it. Never raises."""
        state.update(over)
        await run_progress.set_progress(PROGRESS_KIND, user_id, state)

    if not job_ids:
        await report(phase="done", finished_at=datetime.now(timezone.utc).isoformat())
        return {"proposed": 0, "jobs": {}, "errors": {}, "llm_usage": {}}

    await report(phase="starting")

    try:
        resume_wrap = (await mcp_client.resume_mcp(user_id, "get_base_resume", {}))["resume"]
        if not resume_wrap or not resume_wrap.get("structured"):
            raise RuntimeError("no uploaded resume on file — upload a resume before proposing")
        resume = resume_wrap["structured"]
    except Exception as exc:  # noqa: BLE001 - surface the reason instead of dying silently
        await report(phase="failed", error=f"{type(exc).__name__}: {exc}",
                     finished_at=datetime.now(timezone.utc).isoformat())
        raise

    usage = LLMUsage(model=settings.enhancer_model)
    errors: dict[str, str] = {}
    jobs_out: dict[str, dict] = {}

    # A failed analyze step used to propagate and bin the whole run, including the jobs that
    # were already analysed and needed nothing from it. Now it's recorded and the run goes on
    # with whatever analyses exist.
    try:
        analyses = await _ensure_analyses(user_id, job_ids, report=report)
    except Exception as exc:  # noqa: BLE001
        logger.warning("gap_close.analyze step failed, continuing with existing analyses: %s", exc)
        errors["_analyze"] = f"{type(exc).__name__}: {exc}"
        analyses = await _analyses_by_job(user_id)

    await report(phase="refining" if refine else "proposing", errors=errors)

    for jid in job_ids:
        analysis = analyses.get(jid)
        if not analysis:
            errors[jid] = "no analysis could be produced for this job"
            await report(done=len(jobs_out), errors=errors)
            continue
        try:
            res = await gap_closer.propose_gap_close(analysis, resume, settings)
            if not res.data:
                errors[jid] = "gap-closer returned no proposals"
                await report(done=len(jobs_out), errors=errors)
                continue
            usage.add(res.usage.input_tokens, res.usage.output_tokens,
                      cache_read=res.usage.cache_read_input_tokens,
                      cache_creation=res.usage.cache_creation_input_tokens)
            shaped = gap_closer.with_item_ids(res.data)

            if refine:
                shaped = await _refine(
                    shaped, analysis, resume, settings, usage, jid, errors
                )
            await mcp_client.db_mcp(user_id, "upsert_gap_close",
                                    {"job_id": jid, "gap_close": shaped})
            jobs_out[jid] = shaped
        except Exception as exc:  # noqa: BLE001 - one bad job must not sink the batch
            errors[jid] = f"{type(exc).__name__}: {exc}"
            logger.warning("gap_close.job id=%s FAILED: %s", jid, exc)
        await report(phase="refining" if refine else "proposing",
                     done=len(jobs_out), errors=errors)

    summary = {
        "proposed": len(jobs_out),
        "jobs": jobs_out,
        "errors": errors,
        "llm_usage": usage.as_dict(),
    }
    logger.info("gap_close.summary jobs=%d errors=%d cost=%.6f",
                len(jobs_out), len(errors), usage.cost_usd)
    await report(phase="done", done=len(jobs_out), errors=errors,
                 cost_usd=usage.cost_usd,
                 finished_at=datetime.now(timezone.utc).isoformat())
    return summary
