"""Analyzer orchestration (Phase 5, 5a/5b/5c).

For a scope (selected ``job_ids`` or all qualified jobs): read resume + jobs via MCP,
run the Analyzer over them, and persist per-job ``analyses`` via db-mcp. Calls are
**synchronous** by default; set ``analyze_batch_threshold`` above 0 to route runs of at
least that many jobs through the **Batch API** (50% off, but asynchronous — it bought ~5c
on a 20-job run at the cost of ~40 minutes, so it is off). Then a **deterministic rollup** (no LLM) into an
aggregate summary. Reads resume via resume-mcp, jobs via db-mcp; never a direct ORM
write. ``user_id`` from the trusted context. Per-job error isolation.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

from jobpilot_core import analyzer, events, tracing
from jobpilot_core.llm import (
    LLMUsage,
    collect_batch_results,
    get_batch_status,
    submit_batch,
)
from jobpilot_core.settings import get_settings

from . import mcp_client

logger = logging.getLogger("jobpilot.analyze")


async def _emit(user_id: uuid.UUID, event: dict) -> None:
    try:
        await events.publish_event(get_settings().redis_url, user_id, event)
        await events.beat(get_settings().redis_url, user_id, event.get("stage", "analyze"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("analyze.emit failed: %s", exc)


def _limited(job: dict) -> bool:
    return job.get("description_completeness") == "snippet"


async def _select_jobs(user_id: uuid.UUID, job_ids: list[str] | None, all_qualified: bool) -> list[dict]:
    jobs = (await mcp_client.db_mcp(user_id, "get_qualified_jobs", {"limit": 5000}))["jobs"]
    if job_ids:
        wanted = set(job_ids)
        return [j for j in jobs if j["id"] in wanted]
    if all_qualified:
        return [j for j in jobs if (j.get("qualification") or {}).get("decision") == "qualified"]
    return []


async def _persist(user_id: uuid.UUID, job: dict, data: dict, batch_id: str) -> dict:
    """Store one analysis; stamp limited_data inside extracted_json. Returns the
    extracted analysis (with limited_data) for the deterministic rollup."""
    extracted = {**data, "limited_data": _limited(job)}
    await mcp_client.db_mcp(user_id, "upsert_analysis", {
        "job_id": job["id"],
        "extracted_json": extracted,
        "gaps_json": {"gaps": data.get("gaps_vs_resume") or []},
        "seniority": data.get("seniority"),
        "batch_id": batch_id,
    })
    return extracted


async def _run_sync(user_id, jobs, resume, settings, batch_id, usage, errors) -> list[dict]:
    analyses: list[dict] = []
    for job in jobs:
        try:
            res = await analyzer.analyze(job, resume, settings)
            if not res.data:
                errors[job["id"]] = "empty analysis"
                continue
            usage.add(res.usage.input_tokens, res.usage.output_tokens,
                      cache_read=res.usage.cache_read_input_tokens,
                      cache_creation=res.usage.cache_creation_input_tokens)
            analyses.append(await _persist(user_id, job, res.data, batch_id))
        except Exception as exc:  # noqa: BLE001 - one bad job must not kill the run
            errors[job["id"]] = f"{type(exc).__name__}: {exc}"
            logger.warning("analyze.job id=%s FAILED: %s", job["id"], exc)
    return analyses


async def _run_batch(user_id, jobs, resume, settings, batch_id, usage, errors,
                     *, unbounded: bool = False, on_wait=None) -> list[dict]:
    by_id = {j["id"]: j for j in jobs}
    requests = [
        {"custom_id": j["id"], "params": analyzer.batch_params(j, resume, settings)}
        for j in jobs
    ]
    bid = await submit_batch(settings, requests)
    # ``unbounded`` drops OUR deadline and waits for the batch to reach a terminal state. That
    # is still guaranteed to end: Anthropic expires a batch 24h after creation, and an expired
    # batch reports "ended" like any other, so the loop always terminates on its own.
    deadline = None if unbounded else time.monotonic() + settings.batch_max_wait_seconds
    waited = 0.0
    while True:
        batch_status = await get_batch_status(settings, bid)
        if batch_status == "ended":
            break
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError(f"batch {bid} did not finish within {settings.batch_max_wait_seconds}s")
        if on_wait is not None:
            await on_wait({"batch_id": bid, "batch_status": batch_status,
                           "waited_seconds": int(waited)})
        await asyncio.sleep(settings.batch_poll_interval_seconds)
        waited += settings.batch_poll_interval_seconds

    results, busage = await collect_batch_results(
        settings, bid, analyzer.ANALYZER_TOOL, settings.analyzer_model
    )
    usage.batch = True
    usage.add(busage.input_tokens, busage.output_tokens,
              cache_read=busage.cache_read_input_tokens,
              cache_creation=busage.cache_creation_input_tokens)
    analyses: list[dict] = []
    for job_id, data in results.items():
        job = by_id.get(job_id)
        if job is None or not data:
            continue
        analyses.append(await _persist(user_id, job, data, batch_id))
    for job_id in by_id:
        if job_id not in results:
            errors[job_id] = "batch: not returned"
    return analyses


async def run_analyze(
    user_id: uuid.UUID,
    job_ids: list[str] | None = None,
    all_qualified: bool = False,
    pipeline_id: uuid.UUID | str | None = None,
    *,
    unbounded_batch: bool = False,
    on_wait=None,
) -> dict:
    """``unbounded_batch`` removes the batch-wait deadline for callers that would rather wait
    than lose the run (gap-close). ``on_wait`` is awaited on every poll while a batch is still
    running, so a caller can surface "still waiting" to the user."""
    settings = get_settings()
    pid = str(pipeline_id) if pipeline_id else None
    batch_id = str(uuid.uuid4())  # correlates all analyses from this run

    resume_wrap = (await mcp_client.resume_mcp(user_id, "get_structured_resume", {}))["resume"]
    if not resume_wrap or not resume_wrap.get("structured"):
        raise RuntimeError("no resume on file — upload a resume before analyzing")
    resume = resume_wrap["structured"]

    jobs = (await _select_jobs(user_id, job_ids, all_qualified))[: settings.analyze_max_jobs]
    await _emit(user_id, events.make_event(
        "analyze", events.STARTED, pipeline_id=pid, counts={"total": len(jobs)}))
    if not jobs:
        return {"analyzed": 0, "mode": "none", "limited_data": 0, "aggregate": None,
                "errors": {}, "llm_usage": LLMUsage(model=settings.analyzer_model).as_dict()}

    usage = LLMUsage(model=settings.analyzer_model)
    errors: dict[str, str] = {}
    threshold = settings.analyze_batch_threshold
    mode = "batch" if threshold > 0 and len(jobs) >= threshold else "sync"
    if mode == "batch":
        analyses = await _run_batch(user_id, jobs, resume, settings, batch_id, usage, errors,
                                    unbounded=unbounded_batch, on_wait=on_wait)
    else:
        analyses = await _run_sync(user_id, jobs, resume, settings, batch_id, usage, errors)

    aggregate = analyzer.aggregate(analyses)
    summary = {
        "analyzed": len(analyses),
        "mode": mode,
        "batch_id": batch_id,
        "limited_data": sum(1 for a in analyses if a.get("limited_data")),
        "aggregate": aggregate,
        "errors": errors,
        "llm_usage": usage.as_dict(),
    }
    logger.info("analyze.summary analyzed=%d mode=%s cost=%.6f savings_pct=%s",
                len(analyses), mode, usage.cost_usd, usage.as_dict().get("savings_pct"))
    await _emit(user_id, events.make_event("analyze", events.COMPLETED, pipeline_id=pid, counts=summary))
    return summary


@tracing.traced_stage("analyze")
async def run_analyze_tracked(
    user_id: uuid.UUID,
    job_ids: list[str] | None = None,
    all_qualified: bool = False,
    pipeline_id: uuid.UUID | str | None = None,
) -> dict:
    """Run Analyze, recording a ``pipeline_runs`` row (stage='analyze')."""
    pid = str(pipeline_id) if pipeline_id else str(uuid.uuid4())
    run = await mcp_client.db_mcp(user_id, "create_pipeline_run", {"stage": "analyze", "pipeline_id": pid})
    run_id = run["id"]
    try:
        summary = await run_analyze(user_id, job_ids=job_ids, all_qualified=all_qualified, pipeline_id=pid)
    except Exception as exc:  # noqa: BLE001
        await mcp_client.db_mcp(user_id, "finish_pipeline_run", {
            "run_id": run_id, "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
        await _emit(user_id, events.make_event(
            "analyze", events.FAILED, pipeline_id=pid, error=f"{type(exc).__name__}: {exc}"))
        raise
    errors = summary.get("errors") or {}
    err_str = f"{len(errors)} job error(s)" if errors else None
    await mcp_client.db_mcp(user_id, "finish_pipeline_run", {
        "run_id": run_id, "status": "completed", "counts": summary, "error": err_str})
    summary["run_id"] = run_id
    summary["pipeline_id"] = pid
    from .embed_runner import enqueue_embed  # embed-on-write: new analyses (Phase 7)
    enqueue_embed(user_id, ["analyses"])
    return summary
