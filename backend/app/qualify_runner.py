"""Qualifier orchestration — hybrid scoring over the user's job pool.

For each job: deterministic score → must-have gate → (gate-passers only) Haiku
refine → combine → persist via **db-mcp** ``upsert_qualification``. Reads resume via
resume-mcp, configs via config-mcp, jobs via db-mcp. No direct DB. ``user_id`` comes
from the trusted context, never from job text. Per-job error isolation; scores only the
UNSCORED by default (``requalify_all=True`` re-scores the whole pool).
"""

from __future__ import annotations

import logging
import uuid

from jobpilot_core import events, tracing
from jobpilot_core.qualifier import (
    combine,
    refine_with_llm,
    score_deterministic,
    select_effective_resume,
)
from jobpilot_core.settings import get_settings

from . import mcp_client

logger = logging.getLogger("jobpilot.qualify")

# Emit a qualify.progress event at most this often (jobs) — keeps the live UI
# moving without flooding the bus on a big pool.
_PROGRESS_EVERY = 10


async def _emit(user_id: uuid.UUID, event: dict) -> None:
    try:
        await events.publish_event(get_settings().redis_url, user_id, event)
    except Exception as exc:  # noqa: BLE001
        logger.warning("qualify.emit failed: %s", exc)
    # Liveness, not delivery: the event bus is pub/sub and drops what nobody is listening
    # to, whereas the reaper has to be able to tell "still working" from "worker died".
    await events.beat(get_settings().redis_url, user_id, event.get("stage", "qualify"))


async def run_qualify(
    user_id: uuid.UUID, requalify_all: bool = False, pipeline_id: uuid.UUID | str | None = None
) -> dict:
    settings = get_settings()
    pid = str(pipeline_id) if pipeline_id else None
    # Parent = the latest UPLOADED resume (not an assembled apply-to-resume version), so
    # applying suggestions can never take over qualification — it's just a downloadable file.
    resume_wrap = (await mcp_client.resume_mcp(user_id, "get_base_resume", {}))["resume"]
    if not resume_wrap or not resume_wrap.get("structured"):
        raise RuntimeError("no uploaded resume on file — upload a resume before qualifying")
    resume = resume_wrap["structured"]
    resume_version = resume_wrap.get("id")

    qcfg = (await mcp_client.config_mcp(user_id, "get_qualifier_config", {}))["config"]
    config_version = qcfg.get("id") or "default"

    jobs = (await mcp_client.db_mcp(user_id, "get_qualified_jobs", {"limit": 5000}))["jobs"]

    # CV-aware scoring: a job with an exported tailored CV (built from the CURRENT parent
    # resume) is scored against that CV; every other job against the parent. Keep the newest
    # approved draft per job that is actually ATTACHED — exported (pdf_ref) AND built from the
    # current parent — so this matches exactly what the dashboard row shows (no display/score
    # divergence). db-mcp returns newest-first.
    drafts = (await mcp_client.db_mcp(user_id, "get_cv_drafts", {"status": "approved"}))["drafts"]
    draft_by_job: dict[str, dict] = {}
    for d in drafts:
        jid = d.get("job_id")
        if (jid and jid not in draft_by_job and d.get("pdf_ref")
                and str(d.get("base_resume_version")) == str(resume_version)):
            draft_by_job[jid] = d

    counts = {"scored": 0, "qualified": 0, "disqualified": 0, "gated_out": 0, "skipped": 0}
    errors: dict[str, str] = {}
    usage = {"input_tokens": 0, "output_tokens": 0, "estimated_cost_usd": 0.0}

    # Resolve the work set BEFORE reporting a total.
    #
    # A job that has ALREADY been scored is skipped: qualify does the jobs nothing has ever
    # looked at, and nothing else. That makes an interrupted run cheap to resume, and it
    # makes the routine loop — scout brings in new roles, qualify scores them — cost what
    # the new roles cost instead of re-billing the whole pool.
    #
    # This deliberately does NOT chase staleness. The scoring key (effective CV + config) is
    # still recorded on every qualification, but it is no longer consulted here, because
    # re-uploading a CV mints a new resume id and thereby invalidated every score in the
    # pool — one upload turned the next qualify into a full re-run of work already paid for.
    # When a re-score IS wanted (a tightened config, a genuinely better CV) it is asked for
    # explicitly via requalify_all, which is what the qualifier-settings screen already
    # sends after a config change.
    #
    # `total` counts the work actually left to do rather than the pool, so a resumed run
    # does not announce "0 of 1189" and look like it is starting over.
    todo: list[dict] = []
    for job in jobs:
        if not requalify_all and job.get("qualification"):
            counts["skipped"] += 1
            continue
        todo.append(job)
    # One run is capped; the rest stay unscored and are picked up by the next run.
    if len(todo) > settings.qualify_max_jobs:
        counts["skipped"] += len(todo) - settings.qualify_max_jobs
        todo = todo[: settings.qualify_max_jobs]

    total = len(todo)
    await _emit(user_id, events.make_event(
        "qualify", events.STARTED, pipeline_id=pid,
        counts={"total": total, "already_scored": counts["skipped"], "pool": len(jobs)}))

    for processed, job in enumerate(todo, start=1):
        try:
            eff_resume, cv_source, cv_version = select_effective_resume(
                resume, resume_version, draft_by_job.get(job["id"]))

            det = score_deterministic(eff_resume, job, qcfg)
            llm_data = None
            if det["must_have_gate"] == "pass":
                res = await refine_with_llm(eff_resume, job, qcfg, settings)  # gate-passers only
                llm_data = res.data
                usage["input_tokens"] += res.usage.input_tokens
                usage["output_tokens"] += res.usage.output_tokens
                usage["estimated_cost_usd"] += res.usage.cost_usd
            else:
                counts["gated_out"] += 1  # hard-fail: NO llm call made

            final = combine(det, llm_data, qcfg)
            # Addendum 2.2: a job scored on aggregator `snippet` text (a teaser, not the
            # full body) is low-confidence. We flag it — but neither skip nor down-weight
            # it — so the UI can be honest without silently distorting the score. `full`
            # jobs are scored normally and carry no flag.
            limited_data = job.get("description_completeness") == "snippet"
            reasons_json = {
                "dimension_scores": final["dimension_scores"],
                "weighted_score": final["score"],
                "roles_match": final["roles_match"],
                "gate": det["must_have_gate"],
                "reasons": final["reasons"],
                "limited_data": limited_data,
                "resume_version": resume_version,
                "config_version": config_version,
                "cv_source": cv_source,      # tailored draft id, or "parent"
                "cv_version": cv_version,    # re-score key: effective-CV identity
            }
            await mcp_client.db_mcp(user_id, "upsert_qualification", {
                "job_id": job["id"], "score": final["score"], "decision": final["decision"],
                "reasons_json": reasons_json,
                "matched_skills": final["matched_skills"], "missing_skills": final["missing_skills"],
            })
            counts["scored"] += 1
            counts[final["decision"]] += 1
        except Exception as exc:  # noqa: BLE001 - one bad job must not kill the run
            errors[str(job.get("id", "?"))] = f"{type(exc).__name__}: {exc}"
            logger.warning("qualify.job id=%s FAILED: %s", job.get("id"), exc)
        if processed % _PROGRESS_EVERY == 0 or processed == total:
            await _emit(user_id, events.make_event(
                "qualify", events.PROGRESS, pipeline_id=pid,
                counts={"processed": processed, "total": total,
                        "qualified": counts["qualified"], "disqualified": counts["disqualified"]}))

    summary = {
        **counts,
        "errors": errors,
        "llm_usage": {**usage, "estimated_cost_usd": round(usage["estimated_cost_usd"], 6)},
        "resume_version": resume_version,
        "config_version": config_version,
    }
    logger.info("qualify.summary %s", {k: summary[k] for k in ("scored", "qualified", "disqualified", "gated_out", "skipped")})
    return summary


@tracing.traced_stage("qualify")
async def run_qualify_tracked(
    user_id: uuid.UUID, requalify_all: bool = False, pipeline_id: uuid.UUID | str | None = None
) -> dict:
    """Run Qualify, recording a ``pipeline_runs`` row (stage='qualify') correlated
    under ``pipeline_id``, and emitting ``qualify.started/progress/completed/failed``."""
    pid = str(pipeline_id) if pipeline_id else str(uuid.uuid4())
    run = await mcp_client.db_mcp(
        user_id, "create_pipeline_run", {"stage": "qualify", "pipeline_id": pid}
    )
    run_id = run["id"]
    try:
        summary = await run_qualify(user_id, requalify_all=requalify_all, pipeline_id=pid)
    except Exception as exc:  # noqa: BLE001
        await mcp_client.db_mcp(user_id, "finish_pipeline_run", {
            "run_id": run_id, "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
        await _emit(user_id, events.make_event(
            "qualify", events.FAILED, pipeline_id=pid, error=f"{type(exc).__name__}: {exc}"))
        raise
    errors = summary.get("errors") or {}
    err_str = f"{len(errors)} job error(s)" if errors else None
    await mcp_client.db_mcp(user_id, "finish_pipeline_run", {
        "run_id": run_id, "status": "completed", "counts": summary, "error": err_str})
    summary["run_id"] = run_id
    summary["pipeline_id"] = pid
    await _emit(user_id, events.make_event(
        "qualify", events.COMPLETED, pipeline_id=pid, counts=summary))
    return summary
