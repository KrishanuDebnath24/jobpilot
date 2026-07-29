"""CV alignment orchestration — runs the Rewriter⇄Critic loop in the Celery worker.

For each user-**selected** job: make sure that job has a Phase-5 analysis (analysing the
ones that don't, so this entry point is self-sufficient), pull the base resume + locked
snapshot, then run :func:`jobpilot_core.cv_loop.align_cv` until the Critic calls the CV
aligned or the round cap is reached. The loop's output is already honest by construction
(``cv_fields.finalize`` runs inside every round), and the per-round audit trail — scores,
issues, integrity flags, and which skills were added or code-rejected — is persisted on
the draft so a reviewer can see exactly how the CV got there.

Reads via MCP; writes drafts via db-mcp. CV content is never logged.
"""

from __future__ import annotations

import logging
import uuid

from jobpilot_core import cv_fields
from jobpilot_core.cv_loop import align_cv
from jobpilot_core.settings import get_settings

from . import mcp_client

logger = logging.getLogger("jobpilot.cv_align")


async def _base_resume(user_id: uuid.UUID) -> tuple[dict, str | None]:
    # The WORKING resume = the latest uploaded file (not an assembled apply-to-resume
    # version), so an aligned CV is tagged with the same parent id the qualifier scores
    # against — keeping the two in lockstep across apply-to-resume.
    wrap = (await mcp_client.resume_mcp(user_id, "get_base_resume", {}))["resume"]
    if not wrap or not wrap.get("structured"):
        raise RuntimeError("no uploaded resume on file — upload a resume before tailoring")
    return wrap["structured"], wrap.get("id")


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


async def _jobs_by_id(user_id: uuid.UUID) -> dict[str, dict]:
    jobs = (await mcp_client.db_mcp(user_id, "get_qualified_jobs", {"limit": 5000}))["jobs"]
    return {j["id"]: j for j in jobs}


async def _ensure_analyses(user_id: uuid.UUID, job_ids: list[str]) -> dict[str, dict]:
    """Analyses for the selection, analysing any job that doesn't have one yet. The
    Analyzer already scopes to selected ids, so this costs only the missing jobs."""
    analyses = await _analyses_by_job(user_id)
    missing = [jid for jid in job_ids if jid not in analyses]
    if not missing:
        return analyses
    from .analyze_runner import run_analyze

    logger.info("cv_align.analyzing %d selected job(s) with no analysis", len(missing))
    await run_analyze(user_id, job_ids=missing)
    return await _analyses_by_job(user_id)


def _notes(result: dict, cv: dict, base: dict, settings) -> dict:
    """The review trail persisted on the draft — how aligned, over how many rounds, and
    what was added or rejected along the way."""
    critique = result.get("critique") or {}
    return {
        "aligned": result["aligned"],
        "alignment_score": result["alignment_score"],
        "threshold": settings.cv_alignment_threshold,
        "rounds_run": len(result["rounds"]),
        "max_rounds": settings.cv_max_rounds,
        "rounds": result["rounds"],
        "final_verdict": critique.get("verdict"),
        "dimension_scores": critique.get("dimension_scores"),
        "matched_requirements": critique.get("matched_requirements") or [],
        "unaddressed_requirements": critique.get("unaddressed_requirements") or [],
        "integrity_flags": critique.get("integrity_flags") or [],
        "added_skills": cv_fields.added_skills(cv, base),
        "skill_evidence": cv.get("skill_evidence") or [],
        "change_notes": cv.get("change_notes") or [],
    }


async def run_align(user_id: uuid.UUID, job_ids: list[str]) -> dict:
    """Align a CV to each selected job via the Rewriter⇄Critic loop. Persists one
    ``draft_ready`` cv_draft per job. Per-job error isolation; bounded selection."""
    settings = get_settings()
    job_ids = list(dict.fromkeys(job_ids))[: settings.cv_tailor_max_jobs]
    if not job_ids:
        return {"aligned": 0, "tailored": 0, "drafts": [], "errors": {}, "llm_usage": {}}

    base, base_version = await _base_resume(user_id)
    snapshot = cv_fields.extract_locked_snapshot(base)
    analyses = await _ensure_analyses(user_id, job_ids)
    jobs = await _jobs_by_id(user_id)

    drafts: list[dict] = []
    errors: dict[str, str] = {}
    cost = 0.0
    rounds_total = 0
    fully_aligned = 0

    for jid in job_ids:
        job = jobs.get(jid)
        if job is None:
            errors[jid] = "job not found"
            continue
        analysis = analyses.get(jid)
        if not analysis:
            errors[jid] = "no analysis could be produced for this job"
            continue
        try:
            result = await align_cv(base, snapshot, job, analysis, settings)
            cv = result["cv"]
            cost += sum(u.cost_usd for u in result["usage"].values())
            rounds_total += len(result["rounds"])
            fully_aligned += 1 if result["aligned"] else 0

            saved = await mcp_client.db_mcp(user_id, "create_cv_draft", {
                "job_id": jid,
                "tailored_content_json": cv,
                "locked_snapshot_json": snapshot,
                "base_resume_version": base_version,
                "critic_notes_json": _notes(result, cv, base, settings),
                "before_score": (job.get("qualification") or {}).get("score"),
            })
            drafts.append({
                "job_id": jid,
                "draft_id": saved["draft"]["id"],
                "aligned": result["aligned"],
                "alignment_score": result["alignment_score"],
                "rounds": len(result["rounds"]),
            })
        except Exception as exc:  # noqa: BLE001 - one bad job must not sink the batch
            errors[jid] = f"{type(exc).__name__}: {exc}"
            logger.warning("cv_align.job id=%s FAILED: %s", jid, exc)

    summary = {
        "tailored": len(drafts),          # drafts produced (the historical key)
        "aligned": fully_aligned,         # how many cleared the alignment bar
        "drafts": drafts,
        "errors": errors,
        "llm_usage": {
            "rewriter_model": settings.rewriter_model,
            "critic_model": settings.cv_critic_model,
            "rounds_total": rounds_total,
            "estimated_cost_usd": round(cost, 6),
        },
    }
    logger.info("cv_align.summary drafts=%d aligned=%d rounds=%d errors=%d cost=%.6f",
                len(drafts), fully_aligned, rounds_total, len(errors), cost)
    return summary


# Backwards-compatible alias: the Celery task, the Concierge tool and the Prepare-CVs
# chain all speak in terms of "tailor", which is now the whole align→format pipeline.
run_tailor = run_align
